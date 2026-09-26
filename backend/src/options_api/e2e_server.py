from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn

from options_api.main import create_app
from options_api.nasdaq import HTTP_TIMEOUT

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


app = create_app(
    client_factory=create_mock_client,
    clock=lambda: NOW,
    predictive_refresh=False,
)


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
