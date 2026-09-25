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


def test_prefetch_fetches_the_universe_before_the_first_request() -> None:
    calls = {"n": 0}
    app = create_app(
        client_factory=_client_factory(_screener, calls),
        prefetch_universe=True,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        universe = app.state.universe
        for _ in range(100):
            if universe.available:
                break
            time.sleep(0.02)
        assert universe.available
        assert calls["n"] == 1
        response = client.get("/api/tickers", params={"q": "IREN"})
        assert response.status_code == 200
        assert calls["n"] == 1


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
