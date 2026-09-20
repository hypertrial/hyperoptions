from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn

from options_api import main as main_module
from options_api import nasdaq as nasdaq_module
from options_api.cache import TickerCache
from options_api.main import app
from options_api.models import TickerListing
from options_api.nasdaq import HTTP_TIMEOUT
from options_api.parser import parse_screener_listings
from options_api.service import OptionChainService
from options_api.universe import TickerUniverse

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "nasdaq_iren_sample.json"
)
SCREENER = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "nasdaq_screener_sample.json"
)
NOW = datetime(2026, 9, 11, 14, tzinfo=UTC)


def _history_payload() -> dict:
    rows = []
    start = date(2025, 9, 11)
    for index in range(370):
        day = start + timedelta(days=index)
        rows.append(
            {
                "date": day.strftime("%m/%d/%Y"),
                "close": "$45.00",
                "low": "$40.00",
            }
        )
    return {"data": {"tradesTable": {"rows": rows}}, "status": {"rCode": 200}}


def _info_payload() -> dict:
    return {
        "data": {
            "marketStatus": "Market",
            "primaryData": {
                "bidPrice": "$49.90",
                "askPrice": "$50.10",
                "lastTradeTimestamp": "Sep 11, 2026 10:00 AM ET",
                "isRealTime": True,
            },
        },
        "status": {"rCode": 200},
    }


def _options_unavailable() -> dict:
    return {
        "data": {"totalRecord": 0, "lastTrade": None, "table": {"rows": None}},
        "message": "Options are not available for this symbol",
        "status": {"rCode": 200},
    }


def _symbol_missing() -> dict:
    return {
        "data": None,
        "status": {
            "rCode": 400,
            "bCodeMessage": [{"code": 1001, "errorMessage": "Symbol not exists."}],
        },
    }


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "screener/stocks" in url:
        return httpx.Response(200, json=json.loads(SCREENER.read_text()))
    if "option-chain" in url:
        if "/quote/WULF/" in url:
            return httpx.Response(500)
        if "/quote/NOOPT/" in url:
            return httpx.Response(200, json=_options_unavailable())
        if "/quote/NONE/" in url:
            return httpx.Response(200, json=_symbol_missing())
        return httpx.Response(200, json=json.loads(FIXTURE.read_text()))
    if "/info" in url:
        return httpx.Response(200, json=_info_payload())
    if "/historical" in url:
        return httpx.Response(200, json=_history_payload())
    raise AssertionError(url)


def create_mock_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
    )


def _seeded_universe(client: httpx.AsyncClient) -> TickerUniverse:
    universe = TickerUniverse(client)
    listings = parse_screener_listings(json.loads(SCREENER.read_text()))
    if not listings:
        listings = [
            TickerListing(symbol="CIFR", name="Cipher Mining Inc."),
            TickerListing(symbol="IREN", name="Iris Energy Limited"),
            TickerListing(symbol="NBIS", name="Nebius Group N.V."),
            TickerListing(symbol="NONE", name="Missing Symbol Inc."),
            TickerListing(symbol="NOOPT", name="No Options Corp."),
            TickerListing(symbol="WULF", name="TeraWulf Inc."),
        ]
    universe.seed(listings, as_of=NOW)
    return universe


def install_mock_service() -> None:
    nasdaq_module.create_http_client = create_mock_client
    main_module.create_http_client = create_mock_client
    client = create_mock_client()
    app.state.http_client = client
    app.state.now = NOW
    app.state.service = OptionChainService(
        client=client,
        cache=TickerCache(ttl_seconds=30, max_entries=64),
        info_cache=TickerCache(ttl_seconds=30, max_entries=64),
        history_cache=TickerCache(ttl_seconds=86_400, max_entries=64),
    )
    app.state.universe = _seeded_universe(client)


@asynccontextmanager
async def e2e_lifespan(_app):
    install_mock_service()
    try:
        yield
    finally:
        client = getattr(app.state, "http_client", None)
        if client is not None:
            await client.aclose()


app.router.lifespan_context = e2e_lifespan


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
