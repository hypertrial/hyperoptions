"""Integration boundaries for StockSweeper routes inside the HyperOptions app."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from options_api.main import create_app
from options_api.models import CoveredCallPage, TickerListing
from stocksweeper.config import Settings
from stocksweeper.storage.db import connect


def _offline_client() -> httpx.AsyncClient:
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network request: {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(fail))


@pytest.fixture
def api(tmp_path):
    app = create_app(
        client_factory=_offline_client,
        prefetch_universe=False,
        research_settings=Settings(data_dir=tmp_path),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.universe.seed([TickerListing(symbol="IREN", name="Iris Energy")])
        yield app, client


def test_one_app_exposes_chain_and_namespaced_research(api) -> None:
    _, client = api
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/research/config").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/covered-calls/{ticker}" in paths
    assert "/api/research/backtest/run" in paths
    assert "/api/research/data/update" in paths
    assert "/api/jobs/{job_id}" in paths
    assert "/api/research/health" not in paths
    assert "/api/config" not in paths


@pytest.mark.parametrize("path", ["/api/research/data/update", "/api/research/backtest/run"])
def test_research_writes_reject_hostile_host_and_origin(api, path: str) -> None:
    _, client = api
    assert (
        client.post(
            path,
            json={},
            headers={"Host": "evil.example", "Origin": "http://localhost:5173"},
        ).status_code
        == 400
    )
    assert client.post(path, json={}).status_code == 403
    assert (
        client.post(path, json={}, headers={"Origin": "http://evil.example"}).status_code
        == 403
    )
    assert (
        client.post(
            path,
            content="{}",
            headers={"Origin": "http://localhost:5173", "Content-Type": "text/plain"},
        ).status_code
        == 415
    )
    assert (
        client.get(
            "/api/research/config", headers={"Origin": "http://evil.example"}
        ).status_code
        == 403
    )
    preflight = client.options(
        path,
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 200
    assert "POST" in preflight.headers["access-control-allow-methods"]


def test_research_rejects_unbounded_unknown_and_synthetic_inputs(api, monkeypatch) -> None:
    _, client = api
    monkeypatch.setattr(
        "stocksweeper.pipeline.update.update_market_data",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "stocksweeper.pipeline.sweep.run_sweep",
        lambda *_args, **_kwargs: None,
    )
    headers = {"Origin": "http://localhost:5173"}
    for path in ("/api/research/data/update", "/api/research/backtest/run"):
        for tickers in (["../x"], ["MSFT"], [f"T{i}" for i in range(21)]):
            response = client.post(path, json={"tickers": tickers}, headers=headers)
            assert 400 <= response.status_code < 500, (path, tickers, response.text)
    assert (
        client.post(
            "/api/research/backtest/run", json={"synthetic": True}, headers=headers
        ).status_code
        == 422
    )
    for max_strategies in (0, 20_001):
        assert (
            client.post(
                "/api/research/backtest/run",
                json={"max_strategies": max_strategies},
                headers=headers,
            ).status_code
            == 422
        )
    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_chain_responds_while_research_job_is_running(api, monkeypatch) -> None:
    _, client = api
    started = threading.Event()
    release = threading.Event()

    def slow_update(*_args, **_kwargs) -> None:
        started.set()
        assert release.wait(5)

    async def chain_page(*_args, **_kwargs) -> CoveredCallPage:
        return CoveredCallPage.model_validate(
            {
                "ticker": "IREN",
                "options_available": True,
                "moneyness": "itm",
                "fetched_at": datetime.now(UTC),
                "current_cents": 5000,
                "current_source": "stock_bid",
                "stock_bid_cents": 5000,
                "stock_ask_cents": 5010,
                "market_session": "Market",
                "is_real_time": True,
                "quote_timestamp": None,
                "last_trade": None,
                "last_trade_timestamp": None,
                "truncated": False,
                "chain_from_cache": False,
                "info_from_cache": False,
                "history_from_cache": False,
                "risk_free_rate_pct_tenths": 40,
                "lows": {
                    "d7_cents": None,
                    "d30_cents": None,
                    "d90_cents": None,
                    "d365_cents": None,
                },
                "expirations": [],
            }
        )

    monkeypatch.setattr("stocksweeper.pipeline.update.update_market_data", slow_update)
    monkeypatch.setattr("options_api.main.load_covered_calls", chain_page)
    watchdog = threading.Timer(5, release.set)
    watchdog.start()
    try:
        posted_at = time.monotonic()
        response = client.post(
            "/api/research/data/update",
            json={"tickers": ["IREN"]},
            headers={"Origin": "http://localhost:5173"},
        )
        assert response.status_code == 200
        assert time.monotonic() - posted_at < 2
        assert started.wait(1)
        assert client.get("/api/covered-calls/IREN").status_code == 200
        assert client.get(f"/api/jobs/{response.json()['id']}").status_code == 200
    finally:
        release.set()
        watchdog.cancel()


def test_restart_marks_interrupted_jobs_failed_and_keeps_completed_jobs(tmp_path) -> None:
    now = datetime.now(UTC)
    with connect(tmp_path / "results.duckdb") as connection:
        for state in ("queued", "running", "succeeded"):
            connection.execute(
                """INSERT INTO jobs (id, kind, state, progress, message, created_at, updated_at)
                   VALUES (?, 'data', ?, 0, ?, ?, ?)""",
                [state, state, state, now, now],
            )

    app = create_app(
        client_factory=_offline_client,
        prefetch_universe=False,
        research_settings=Settings(data_dir=tmp_path),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for job_id in ("queued", "running"):
            recovered = client.get(f"/api/jobs/{job_id}")
            assert recovered.status_code == 200
            assert recovered.json()["state"] == "failed"
            assert recovered.json()["message"] == "interrupted"
        assert client.get("/api/jobs/succeeded").json()["state"] == "succeeded"
