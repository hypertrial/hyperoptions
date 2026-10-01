from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from options_api.models import TickerListing
from options_api.nasdaq import NasdaqError
from options_api.parser import parse_screener_listings
from options_api.universe import TickerUniverse


def test_search_prefers_symbol_prefix_then_name() -> None:
    universe = TickerUniverse(httpx.AsyncClient())
    universe.seed(
        [
            TickerListing(symbol="IREN", name="Iris Energy Limited"),
            TickerListing(symbol="IR", name="Ingersoll Rand"),
            TickerListing(symbol="CIFR", name="Cipher Mining Inc."),
            TickerListing(symbol="AAPL", name="Apple Inc."),
        ]
    )
    assert [row.symbol for row in universe.search("IR", 10)] == ["IR", "IREN"]
    assert [row.symbol for row in universe.search("cipher", 10)] == ["CIFR"]
    assert [row.symbol for row in universe.search("iris", 10)] == ["IREN"]
    assert [row.symbol for row in universe.search("", 2)] == ["AAPL", "CIFR"]


def test_seed_drops_invalid_symbols() -> None:
    universe = TickerUniverse(httpx.AsyncClient())
    universe.seed(
        [
            TickerListing(symbol="BRK.A", name="Berkshire"),
            TickerListing(symbol="IREN", name="Iris Energy Limited"),
        ]
    )
    assert universe.contains("IREN")
    assert not universe.contains("BRK.A")


@pytest.mark.asyncio
async def test_stale_universe_survives_failed_refresh() -> None:
    clock = {"now": 0.0}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    universe = TickerUniverse(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        monotonic=lambda: clock["now"],
    )
    universe.seed([TickerListing(symbol="IREN", name="Iris Energy Limited")])
    clock["now"] = 90_000
    assert await universe.ensure() is True
    assert universe.contains("IREN")


@pytest.mark.parametrize("failure", ["provider", "malformed", "empty"])
async def test_stale_universe_failed_refresh_has_a_bounded_cooldown(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    clock = {"now": 0.0}
    calls = 0

    async def fetch(_client):
        nonlocal calls
        calls += 1
        if failure == "provider":
            raise NasdaqError.unavailable()
        if failure == "malformed":
            return {"status": {"rCode": 200}, "data": {}}
        return {"status": {"rCode": 200}, "data": {"rows": []}}

    monkeypatch.setattr("options_api.universe.fetch_screener_payload", fetch)
    async with httpx.AsyncClient() as client:
        universe = TickerUniverse(client, monotonic=lambda: clock["now"])
        stamp = datetime(2026, 9, 11, tzinfo=UTC)
        universe.seed([TickerListing(symbol="IREN", name="Iris Energy")], as_of=stamp)
        clock["now"] = 90_000
        assert await universe.ensure()
        assert await universe.ensure()
        clock["now"] += 59.999
        assert await universe.ensure()
        assert calls == 1
        assert universe.as_of == stamp
        assert universe._stored_at == 0
        assert universe.contains("IREN")
        clock["now"] = 90_060
        assert await universe.ensure()
        assert calls == 2


async def test_failure_cooldown_starts_at_shared_refresh_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetch(_client):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        raise NasdaqError.unavailable()

    monkeypatch.setattr("options_api.universe.fetch_screener_payload", fetch)
    async with httpx.AsyncClient() as client:
        universe = TickerUniverse(client, monotonic=lambda: clock["now"])
        universe.seed([TickerListing(symbol="IREN", name="Iris Energy")])
        clock["now"] = 90_000
        first = asyncio.create_task(universe.ensure())
        await entered.wait()
        second = asyncio.create_task(universe.ensure())
        await asyncio.sleep(0)
        assert calls == 1
        clock["now"] = 90_050
        release.set()
        assert await asyncio.gather(first, second) == [True, True]
        clock["now"] = 90_109.999
        assert await universe.ensure()
        assert calls == 1
        clock["now"] = 90_110
        assert await universe.ensure()
        assert calls == 2


async def test_success_and_seed_clear_the_failure_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}
    calls = 0

    async def fetch(_client):
        nonlocal calls
        calls += 1
        if calls != 2:
            raise NasdaqError.unavailable()
        return {
            "status": {"rCode": 200},
            "data": {"rows": [{"symbol": "AAPL", "name": "Apple"}]},
        }

    monkeypatch.setattr("options_api.universe.fetch_screener_payload", fetch)
    async with httpx.AsyncClient() as client:
        universe = TickerUniverse(client, monotonic=lambda: clock["now"])
        universe.seed([TickerListing(symbol="IREN", name="Iris Energy")])
        clock["now"] = 90_000
        assert await universe.ensure()
        clock["now"] += 60
        assert await universe.ensure()
        assert universe.contains("AAPL")
        assert not universe.contains("IREN")
        assert universe._failed_at is None
        assert await universe.ensure()
        assert calls == 2
        clock["now"] += 86_400
        assert await universe.ensure()
        assert universe._failed_at is not None
        universe.seed([TickerListing(symbol="IREN", name="Iris Energy")])
        assert universe._failed_at is None
        assert await universe.ensure()
        assert calls == 3


async def test_cold_failure_remains_unavailable_and_can_retry_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fetch(_client):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise NasdaqError.unavailable()
        return {
            "status": {"rCode": 200},
            "data": {"rows": [{"symbol": "IREN", "name": "Iris Energy"}]},
        }

    monkeypatch.setattr("options_api.universe.fetch_screener_payload", fetch)
    async with httpx.AsyncClient() as client:
        universe = TickerUniverse(client)
        assert await universe.ensure() is False
        assert universe.as_of is None
        assert universe._failed_at is None
        assert await universe.ensure() is True
        assert calls == 2


async def test_shutdown_cancellation_does_not_record_a_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}
    entered = asyncio.Event()

    async def fetch(_client):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("options_api.universe.fetch_screener_payload", fetch)
    async with httpx.AsyncClient() as client:
        universe = TickerUniverse(client, monotonic=lambda: clock["now"])
        universe.seed([TickerListing(symbol="IREN", name="Iris Energy")])
        clock["now"] = 90_000
        waiter = asyncio.create_task(universe.ensure())
        await entered.wait()
        await universe.close()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert universe._failed_at is None
        assert universe._inflight is None


@pytest.mark.asyncio
async def test_cancelled_waiter_keeps_shared_refresh_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetch(_client: httpx.AsyncClient) -> dict[str, object]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {
            "status": {"rCode": 200},
            "data": {"rows": [{"symbol": "IREN", "name": "Iris Energy Limited"}]},
        }

    monkeypatch.setattr("options_api.universe.fetch_screener_payload", fetch)
    universe = TickerUniverse(httpx.AsyncClient())
    creator = asyncio.create_task(universe.ensure())
    await started.wait()
    creator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creator

    follower = asyncio.create_task(universe.ensure())
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    assert await follower is True
    assert universe.contains("IREN")
    assert universe._inflight is None


def test_parse_screener_listings_filters_regex() -> None:
    payload = {
        "status": {"rCode": 200},
        "data": {
            "rows": [
                {"symbol": "IREN", "name": "Iris Energy Limited", "sector": "Tech"},
                {"symbol": "BRK.A", "name": "Berkshire"},
                {"symbol": "iren", "name": "Duplicate"},
                {"symbol": "??", "name": "Bad"},
            ]
        },
    }
    listings = parse_screener_listings(payload)
    assert [item.symbol for item in listings] == ["IREN"]
    assert listings[0].name == "Iris Energy Limited"


def test_as_of_is_set_on_seed() -> None:
    universe = TickerUniverse(httpx.AsyncClient())
    stamp = datetime(2026, 9, 11, tzinfo=UTC)
    universe.seed([TickerListing(symbol="A", name="Agilent")], as_of=stamp)
    assert universe.as_of == stamp
    assert universe.available
