from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from options_api.main import create_app

from .conftest import load_fixture

SCREENER = load_fixture("nasdaq_screener_sample.json")


def _client_factory(handler, calls: dict[str, int]):
    def factory() -> httpx.AsyncClient:
        def wrapped(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))

    return factory


def _screener(request: httpx.Request) -> httpx.Response:
    if "screener" not in str(request.url):
        raise AssertionError(request.url)
    return httpx.Response(200, json=SCREENER)


def test_prefetch_fetches_the_universe_and_treasury_curve_before_the_first_request() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "screener" in str(request.url):
            return httpx.Response(200, json=SCREENER)
        return httpx.Response(503)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    app = create_app(client_factory=factory, prefetch_universe=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        universe = app.state.universe
        for _ in range(100):
            if universe.available and any("treasury.gov" in url for url in urls):
                break
            time.sleep(0.02)
        assert universe.available
        assert sum("screener" in url for url in urls) == 1
        assert any("treasury.gov" in url for url in urls)
        response = client.get("/api/tickers", params={"q": "IREN"})
        assert response.status_code == 200
        assert sum("screener" in url for url in urls) == 1


def test_page_starts_the_input_flight_before_the_chain_returns(tmp_path, monkeypatch) -> None:
    import threading

    from options_api.models import TickerListing
    from stocksweeper.config import Settings

    from .synthetic import synthetic_context

    chain, info, history, _today, now = synthetic_context()
    started = threading.Event()
    release = threading.Event()

    async def get_chain(_ticker: str):
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return chain

    async def get_info(_ticker: str, _scan_time=None):
        return info

    async def get_history(_ticker: str, _from_date: str):
        return history

    async def treasury(*_args, **_kwargs):
        await asyncio.to_thread(release.wait, 5)
        return None

    async def dividend(_ticker: str, as_of):
        await asyncio.to_thread(release.wait, 5)
        from options_api.market_sources import DividendStatus
        return DividendStatus("nonpayer", as_of)

    monkeypatch.setattr("options_api.market_watch.fetch_treasury_curve", treasury)
    monkeypatch.setattr("options_api.market_watch.fetch_dividend_status", dividend)
    app = create_app(
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(503))
        ),
        clock=lambda: now,
        prefetch_universe=False,
        research_settings=Settings(data_dir=tmp_path),
        predictive_refresh=False,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
        monkeypatch.setattr(app.state.service, "get_chain", get_chain)
        monkeypatch.setattr(app.state.service, "get_info", get_info)
        monkeypatch.setattr(app.state.service, "get_history", get_history)

        def request() -> None:
            client.get(
                "/api/covered-calls/IREN",
                headers={"Origin": "http://127.0.0.1:5173"},
            )

        worker = threading.Thread(target=request)
        worker.start()
        assert started.wait(2)
        assert "IREN" in app.state.market_odds._page_input_flights
        release.set()
        worker.join(5)
        assert not worker.is_alive()


def test_prefetch_can_stay_off_until_a_request() -> None:
    calls = {"n": 0}
    app = create_app(
        client_factory=_client_factory(_screener, calls),
        prefetch_universe=False,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert calls["n"] == 0
        assert client.get("/api/tickers", params={"q": "IREN"}).status_code == 200
        assert calls["n"] == 1


def test_manual_research_routes_are_not_exposed() -> None:
    app = create_app(
        client_factory=_client_factory(_screener, {"n": 0}),
        prefetch_universe=False,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert not any(path.startswith("/api/research/") for path in paths)
        assert "/api/jobs/{job_id}" in paths
        assert client.get("/api/research/config").status_code == 404
        assert client.post(
            "/api/research/backtest/run", json={},
            headers={"Origin": "http://127.0.0.1:5173"},
        ).status_code == 404


@pytest.mark.asyncio
async def test_shutdown_during_a_slow_universe_fetch_finishes_promptly() -> None:
    started = asyncio.Event()

    async def slow(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json=SCREENER)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(slow))

    app = create_app(client_factory=factory, prefetch_universe=True)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=1)
        started_shutdown = time.perf_counter()
    assert time.perf_counter() - started_shutdown < 1
