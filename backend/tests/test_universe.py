from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from options_api.models import TickerListing
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
