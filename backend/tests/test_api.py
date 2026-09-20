from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from options_api.cache import TickerCache
from options_api.main import app
from options_api.models import TickerListing
from options_api.nasdaq import (
    CHAIN_POLICY,
    FetchPolicy,
    HTTP_TIMEOUT,
    NasdaqError,
    _fetch_payload,
    _request_timeout,
)
from options_api.service import OptionChainService
from options_api.universe import TickerUniverse

from .conftest import load_fixture

NOW = datetime(2026, 9, 11, 14, tzinfo=UTC)


def _seed_universe(client: httpx.AsyncClient | None = None) -> TickerUniverse:
    universe = TickerUniverse(client or httpx.AsyncClient())
    universe.seed(
        [
            TickerListing(symbol="CIFR", name="Cipher Mining Inc."),
            TickerListing(symbol="IREN", name="Iris Energy Limited"),
            TickerListing(symbol="NBIS", name="Nebius Group N.V."),
            TickerListing(symbol="WULF", name="TeraWulf Inc."),
        ]
    )
    return universe


def _install_service(handler) -> None:
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
    )
    app.state.service = OptionChainService(
        client=mock_client,
        cache=TickerCache(ttl_seconds=30),
        info_cache=TickerCache(ttl_seconds=30),
        history_cache=TickerCache(ttl_seconds=86_400),
    )
    app.state.universe = _seed_universe(mock_client)


@pytest.fixture
def api() -> Iterator[TestClient]:
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.now = NOW
        yield client


def _info_payload() -> dict:
    return {
        "data": {
            "marketStatus": "Market",
            "primaryData": {
                "bidPrice": "$49.90",
                "askPrice": "$50.10",
                "bidSize": "10",
                "askSize": "12",
                "lastTradeTimestamp": "Sep 11, 2026 10:00 AM ET",
                "isRealTime": True,
            },
        },
        "status": {"rCode": 200},
    }


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


def _handler(fail_info: bool = False, fail_history: bool = False, fail_chain: bool = False):
    payload = load_fixture("nasdaq_iren_sample.json")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "option-chain" in url:
            if fail_chain:
                return httpx.Response(500)
            return httpx.Response(200, json=payload)
        if "/info" in url:
            if fail_info:
                return httpx.Response(500)
            return httpx.Response(200, json=_info_payload())
        if "/historical" in url:
            if fail_history:
                return httpx.Response(500)
            return httpx.Response(200, json=_history_payload())
        raise AssertionError(url)

    return handler


def test_health_and_allowlist(api: TestClient) -> None:
    assert api.get("/api/health").json() == {"ok": True}
    app.state.universe = _seed_universe()
    for ticker in ("AAPL", "MSFT", "NBI"):
        response = api.get(f"/api/covered-calls/{ticker}")
        assert response.status_code == 404
        assert response.json()["detail"] == "Unknown Nasdaq ticker"
    invalid = api.get("/api/covered-calls/NBISXX")
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "Invalid ticker"


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST:8000",
        "localhost:0",
        "localhost:65535",
        "127.0.0.1",
        "127.0.0.1:8000",
        "::1",
        "[::1]",
        "[::1]:65535",
    ],
)
def test_loopback_host_is_accepted(api: TestClient, host: str) -> None:
    response = api.get("/api/health", headers={"Host": host})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "localhost.attacker.example",
        "192.168.1.1",
        "[2001:db8::1]",
        "user@localhost",
        "localhost/path",
        "localhost?",
        "localhost#",
        "localhost:bad",
        "localhost:-1",
        "localhost:65536",
        "[::1]:65536",
        "",
    ],
)
def test_non_local_or_malformed_host_is_rejected_before_api_dispatch(
    api: TestClient, host: str
) -> None:
    response = api.get("/api/health", headers={"Host": host})
    assert response.status_code == 400


def test_duplicate_host_headers_are_rejected(api: TestClient) -> None:
    response = api.get(
        "/api/health",
        headers=[("Host", "localhost"), ("Host", "attacker.example")],
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_missing_host_header_is_rejected() -> None:
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/health",
            "raw_path": b"/api/health",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
            "state": {},
        },
        receive,
        send,
    )

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 400


def test_itm_calls_contract_filters_and_caches(api: TestClient) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _handler()(request)

    _install_service(handler)
    first = api.get("/api/covered-calls/iren")
    second = api.get("/api/covered-calls/IREN")
    assert first.status_code == 200
    body = first.json()
    assert body["ticker"] == "IREN"
    assert body["moneyness"] == "itm"
    assert body["risk_free_rate_pct_tenths"] == 40
    assert body["current_cents"] == 4990
    assert body["current_source"] == "stock_bid"
    assert body["chain_from_cache"] is False
    assert second.json()["chain_from_cache"] is True
    assert [group["expiration"] for group in body["expirations"]] == ["2026-09-18"]
    assert [row["strike_cents"] for row in body["expirations"][0]["contracts"]] == [
        4800,
        4050,
    ]
    priced = body["expirations"][0]["contracts"][0]
    assert priced["stock_cost_cents"] == 499000
    assert priced["premium_cents"] == 5000
    assert priced["outlay_cents"] == 494000
    assert priced["effective_cost_cents"] == 4940
    assert priced["called_pnl_cents"] == -14000
    assert priced["called_pnl_per_share_cents"] == -140
    assert priced["dte"] == 7
    remaining = body["expirations"][0]["contracts"][1]
    assert remaining["stock_cost_cents"] == 499000
    assert remaining["premium_cents"] is None
    assert remaining["outlay_cents"] is None
    assert remaining["effective_cost_cents"] is None
    assert remaining["called_pnl_cents"] is None
    assert remaining["called_pnl_per_share_cents"] is None
    assert remaining["stock_apr_pct_tenths"] is None
    assert remaining["drop_to_breakeven_pct_tenths"] is None
    assert calls["count"] == 3
    assert body["fetched_at"].endswith("Z")


def test_openapi_financial_fields_are_integers() -> None:
    schema = app.openapi()
    floats: list[str] = []

    def walk(node: object, path: str = "") -> None:
        if isinstance(node, dict):
            if node.get("type") == "number" or node.get("format") in {"float", "double"}:
                floats.append(path)
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(schema)
    assert floats == []
    contract_schema = schema["components"]["schemas"]["CoveredCallContract"]
    contract = contract_schema["properties"]
    assert "strike_cents" in contract
    assert "strike" not in contract
    assert contract["strike_cents"]["type"] == "integer"

    def schema_types(prop: dict[str, object]) -> set[str]:
        if "anyOf" in prop:
            return {
                option["type"]
                for option in prop["anyOf"]
                if isinstance(option, dict) and "type" in option
            }
        raw = prop.get("type")
        if isinstance(raw, list):
            return set(raw)
        if isinstance(raw, str):
            types = {raw}
            if prop.get("nullable"):
                types.add("null")
            return types
        return set()

    put_schema = schema["components"]["schemas"]["CashSecuredPutContract"]
    put = put_schema["properties"]
    assert "assigned_pnl" not in contract
    assert "assigned_pnl" not in put
    assert "collateral_cents" in put
    assert "net_collateral_cents" in put
    assert "breakeven_cents" in put
    assert "iv_pct_tenths" in contract
    assert "delta_e4" in contract
    assert schema_types(contract["delta_e4"]) == {"integer", "null"}
    call_params = schema["paths"]["/api/covered-calls/{ticker}"]["get"]["parameters"]
    param_names = {item["name"] for item in call_params}
    assert "risk_free_rate" not in param_names
    assert "r" not in param_names
    assert "called_pnl_per_share_cents" in contract
    assert "called_pnl_per_share_cents" in contract_schema["required"]
    assert schema_types(contract["called_pnl_per_share_cents"]) == {"integer", "null"}
    assert schema_types(contract["called_pnl_cents"]) == {"integer", "null"}
    assert schema_types(contract["stock_cost_cents"]) == {"integer", "null"}
    assert schema_types(contract["premium_cents"]) == {"integer", "null"}
    assert schema_types(contract["stock_apr_pct_tenths"]) == {"integer", "null"}
    assert schema_types(contract["drop_to_breakeven_pct_tenths"]) == {"integer", "null"}
    assert "stock_cost_cents" in contract_schema["required"]
    assert "premium_cents" in contract_schema["required"]
    assert "stock_apr_pct_tenths" in contract_schema["required"]
    assert "drop_to_breakeven_pct_tenths" in contract_schema["required"]


def test_removed_planner_routes_are_gone(api: TestClient) -> None:
    assert api.get("/api/itm-calls/IREN").status_code == 404
    assert api.get("/api/covered-calls").status_code == 404
    assert api.post("/api/covered-calls").status_code == 404
    assert api.get("/api/options/IREN").status_code == 404
    assert api.get("/api/paper-observations").status_code == 404
    assert api.post("/api/paper-observations").status_code == 404
    assert api.get("/api/context-observations").status_code == 404
    assert api.post("/api/context-observations").status_code == 404


def test_allowlisted_tickers_other_than_iren_are_accepted(api: TestClient) -> None:
    _install_service(_handler())
    for ticker in ("CIFR", "NBIS", "WULF"):
        response = api.get(f"/api/covered-calls/{ticker}")
        assert response.status_code == 200
        assert response.json()["ticker"] == ticker


def test_nbis_is_accepted_in_any_case_via_assembler(api: TestClient) -> None:
    nasdaq_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nasdaq_urls.append(str(request.url))
        return _handler()(request)

    _install_service(handler)
    bodies = []
    for raw in ("nbis", "Nbis", "NBIS"):
        response = api.get(f"/api/covered-calls/{raw}")
        assert response.status_code == 200
        body = response.json()
        assert body["ticker"] == "NBIS"
        assert [group["expiration"] for group in body["expirations"]] == ["2026-09-18"]
        assert [row["strike_cents"] for row in body["expirations"][0]["contracts"]] == [
            4800,
            4050,
        ]
        bodies.append(body)

    assert bodies[0]["chain_from_cache"] is False
    assert bodies[1]["chain_from_cache"] is True
    assert bodies[2]["chain_from_cache"] is True
    assert nasdaq_urls
    assert all("/quote/NBIS/" in url for url in nasdaq_urls)
    assert all("/quote/nbis/" not in url for url in nasdaq_urls)
    assert all("/quote/Nbis/" not in url for url in nasdaq_urls)


def test_malformed_chain_payload_is_a_provider_error(api: TestClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            return httpx.Response(200, json={"data": {"table": {}}})
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 502


def test_chain_with_only_nonpositive_strikes_is_a_provider_error(api: TestClient) -> None:
    payload = load_fixture("nasdaq_iren_sample.json")
    for row in payload["data"]["table"]["rows"]:
        if row.get("strike") is not None:
            row["strike"] = "0"

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            return httpx.Response(200, json=payload)
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 502
    assert response.json()["detail"] == "Nasdaq response is malformed"


def test_chain_timeout_retries_then_times_out(api: TestClient) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            calls["count"] += 1
            raise httpx.ReadTimeout("timed out", request=request)
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 504
    assert response.json()["detail"] == "Nasdaq timeout"
    assert calls["count"] == 2


def test_chain_timeout_then_success_uses_second_attempt(api: TestClient) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            calls["count"] += 1
            if calls["count"] == 1:
                raise httpx.ReadTimeout("timed out", request=request)
            return _handler()(request)
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 200
    assert calls["count"] == 2


def test_chain_http_error_is_not_retried(api: TestClient) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            calls["count"] += 1
            return httpx.Response(500)
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 502
    assert calls["count"] == 1


def test_chain_connect_reset_retries_then_succeeds(api: TestClient) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            calls["count"] += 1
            if calls["count"] == 1:
                raise httpx.ConnectError("reset", request=request)
            return _handler()(request)
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 200
    assert calls["count"] == 2


def test_chain_empty_body_is_not_retried(api: TestClient) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            calls["count"] += 1
            return httpx.Response(200, content=b"")
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 502
    assert calls["count"] == 1


def test_chain_malformed_json_is_not_retried(api: TestClient) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            calls["count"] += 1
            return httpx.Response(200, content=b"{not-json")
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 502
    assert calls["count"] == 1


def test_chain_429_is_not_retried_and_sanitizes_retry_after(api: TestClient) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            calls["count"] += 1
            return httpx.Response(429, headers={"Retry-After": "12"})
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 503
    assert response.json()["detail"] == "Nasdaq unavailable"
    assert response.headers.get("retry-after") == "12"
    assert calls["count"] == 1


def test_request_timeout_is_capped_by_remaining_deadline() -> None:
    started = time.monotonic() - (CHAIN_POLICY.deadline_seconds - 0.4)
    timeout = _request_timeout(CHAIN_POLICY, started)
    assert timeout.read == pytest.approx(0.4, abs=0.05)
    assert timeout.connect is not None and timeout.connect <= 0.45


def test_request_timeout_raises_when_deadline_has_elapsed() -> None:
    started = time.monotonic() - CHAIN_POLICY.deadline_seconds
    with pytest.raises(NasdaqError) as caught:
        _request_timeout(CHAIN_POLICY, started)
    assert caught.value.status_code == 504
    assert caught.value.detail == "Nasdaq timeout"


@pytest.mark.asyncio
async def test_fetch_payload_enforces_one_absolute_deadline() -> None:
    policy = FetchPolicy(
        name="test",
        connect_timeout=1.0,
        read_timeout=1.0,
        write_timeout=1.0,
        pool_timeout=1.0,
        deadline_seconds=0.02,
        max_attempts=1,
        backoff_seconds=0.0,
        retryable_exceptions=CHAIN_POLICY.retryable_exceptions,
        retryable_status_codes=CHAIN_POLICY.retryable_status_codes,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NasdaqError) as caught:
            await _fetch_payload("https://example.test", client, {}, policy, "test")

    assert caught.value.status_code == 504
    assert caught.value.detail == "Nasdaq timeout"


def test_chain_deadline_exhaustion_skips_the_second_attempt(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}
    monkeypatch.setattr(
        "options_api.nasdaq.CHAIN_POLICY",
        FetchPolicy(
            name="chain",
            connect_timeout=5.0,
            read_timeout=15.0,
            write_timeout=10.0,
            pool_timeout=5.0,
            deadline_seconds=0.05,
            max_attempts=2,
            backoff_seconds=1.0,
            retryable_exceptions=CHAIN_POLICY.retryable_exceptions,
            retryable_status_codes=CHAIN_POLICY.retryable_status_codes,
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            calls["count"] += 1
            raise httpx.ReadTimeout("timed out", request=request)
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 504
    assert response.json()["detail"] == "Nasdaq timeout"
    assert calls["count"] == 1


def test_info_timeout_still_returns_the_chain(api: TestClient) -> None:
    calls = {"info": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/info" in str(request.url):
            calls["info"] += 1
            raise httpx.ReadTimeout("timed out", request=request)
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 200
    body = response.json()
    assert body["current_cents"] == 4393
    assert body["current_source"] == "chain_last_trade"
    assert calls["info"] == 2


def test_chain_failure_is_a_provider_error(api: TestClient) -> None:
    _install_service(_handler(fail_chain=True))
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 502


def test_info_cache_keeps_ask_across_utc_minute_when_refetch_would_fail(
    api: TestClient,
) -> None:
    calls = {"info": 0}
    payload = load_fixture("nasdaq_iren_sample.json")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "option-chain" in url:
            return httpx.Response(200, json=payload)
        if "/info" in url:
            calls["info"] += 1
            if calls["info"] > 1:
                return httpx.Response(500)
            return httpx.Response(200, json=_info_payload())
        if "/historical" in url:
            return httpx.Response(200, json=_history_payload())
        raise AssertionError(url)

    _install_service(handler)
    app.state.now = datetime(2026, 9, 11, 14, 0, 50, tzinfo=UTC)
    first = api.get("/api/covered-calls/IREN")
    app.state.now = datetime(2026, 9, 11, 14, 1, 5, tzinfo=UTC)
    second = api.get("/api/covered-calls/IREN")

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["current_cents"] == 4990
    assert first_body["current_source"] == "stock_bid"
    assert [row["strike_cents"] for row in first_body["expirations"][0]["contracts"]] == [
        4800,
        4050,
    ]
    assert second_body["current_cents"] == 4990
    assert second_body["current_source"] == "stock_bid"
    assert [row["strike_cents"] for row in second_body["expirations"][0]["contracts"]] == [
        4800,
        4050,
    ]
    assert second_body["info_from_cache"] is True
    assert calls["info"] == 1


def test_info_or_history_failure_still_returns_the_chain(api: TestClient) -> None:
    _install_service(_handler(fail_info=True, fail_history=True))
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 200
    body = response.json()
    assert body["current_cents"] == 4393
    assert body["current_source"] == "chain_last_trade"
    assert body["lows"] == {
        "d7_cents": None,
        "d30_cents": None,
        "d90_cents": None,
        "d365_cents": None,
    }
    assert [row["strike_cents"] for row in body["expirations"][0]["contracts"]] == [4050]


def test_duplicate_history_dates_fail_the_page(api: TestClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/historical" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "tradesTable": {
                            "rows": [
                                {
                                    "date": "09/10/2026",
                                    "close": "$45.00",
                                    "low": "$40.00",
                                },
                                {
                                    "date": "2026-09-10",
                                    "close": "$44.00",
                                    "low": "$39.00",
                                },
                            ]
                        }
                    },
                    "status": {"rCode": 200},
                },
            )
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 502
    assert response.json()["detail"] == "Nasdaq response is malformed"


def test_disallowed_origin_is_rejected_before_nasdaq(api: TestClient) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _handler()(request)

    _install_service(handler)
    response = api.get(
        "/api/covered-calls/IREN", headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403
    assert calls["count"] == 0


def test_assembler_value_error_uses_static_malformed_detail(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args, **_kwargs):
        raise ValueError("duplicate historical date: 2026-09-10")

    monkeypatch.setattr("options_api.main.load_chain", boom)
    app.state.universe = _seed_universe()
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 502
    assert response.json()["detail"] == "Nasdaq response is malformed"


def test_non_numeric_retry_after_is_dropped(api: TestClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            return httpx.Response(
                429, headers={"Retry-After": "Fri, 01 Jan 2027 00:00:00 GMT"}
            )
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 503
    assert response.headers.get("retry-after") is None


def test_ticker_search_and_fail_closed(api: TestClient) -> None:
    app.state.universe = _seed_universe()
    found = api.get("/api/tickers", params={"q": "IRE", "limit": 5})
    assert found.status_code == 200
    body = found.json()
    assert body["total"] == 4
    assert [row["symbol"] for row in body["results"]] == ["IREN"]
    named = api.get("/api/tickers", params={"q": "cipher"})
    assert [row["symbol"] for row in named.json()["results"]] == ["CIFR"]
    def fail(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    empty = TickerUniverse(
        httpx.AsyncClient(transport=httpx.MockTransport(fail))
    )
    app.state.universe = empty
    assert api.get("/api/tickers").status_code == 503
    assert api.get("/api/covered-calls/IREN").status_code == 503
    assert api.get("/api/cash-secured-puts/IREN").status_code == 503
    too_long = api.get("/api/tickers", params={"q": "A" * 33})
    assert too_long.status_code == 422
    assert api.get("/api/tickers", params={"limit": 0}).status_code == 422
    assert api.get("/api/tickers", params={"limit": 21}).status_code == 422


def test_unknown_or_invalid_ticker_never_reaches_nasdaq(api: TestClient) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _handler()(request)

    _install_service(handler)
    assert api.get("/api/covered-calls/F").status_code == 404
    assert api.get("/api/cash-secured-puts/F").status_code == 404
    assert api.get("/api/covered-calls/ir!").status_code == 400
    assert api.get("/api/covered-calls/irenxx").status_code == 400
    assert calls["count"] == 0


def test_options_unavailable_returns_empty_page(api: TestClient) -> None:
    payload = {
        "data": {"totalRecord": 0, "lastTrade": None, "table": {"rows": None}},
        "message": "Options are not available for this symbol",
        "status": {"rCode": 200},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            return httpx.Response(200, json=payload)
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 200
    body = response.json()
    assert body["options_available"] is False
    assert body["expirations"] == []
    assert body["name"] == "Iris Energy Limited"
    puts = api.get("/api/cash-secured-puts/IREN")
    assert puts.status_code == 200
    put_body = puts.json()
    assert put_body["options_available"] is False
    assert put_body["expirations"] == []


def test_symbol_not_exists_is_404(api: TestClient) -> None:
    payload = {
        "data": None,
        "status": {
            "rCode": 400,
            "bCodeMessage": [{"code": 1001, "errorMessage": "Symbol not exists."}],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            return httpx.Response(400, json=payload)
        return _handler()(request)

    _install_service(handler)
    response = api.get("/api/covered-calls/IREN")
    assert response.status_code == 404
    assert response.json()["detail"] == "Symbol not exists"


def test_cash_secured_puts_otm_default(api: TestClient) -> None:
    _install_service(_handler())
    response = api.get("/api/cash-secured-puts/IREN")
    assert response.status_code == 200
    body = response.json()
    assert body["moneyness"] == "otm"
    assert body["ticker"] == "IREN"
    current = body["current_cents"]
    for group in body["expirations"]:
        for row in group["contracts"]:
            assert row["strike_cents"] <= current
            assert row["in_the_money"] is False
            assert row["put_open_interest"] >= 5


def test_invalid_moneyness_is_rejected(api: TestClient) -> None:
    _install_service(_handler())
    response = api.get("/api/covered-calls/IREN", params={"moneyness": "foo"})
    assert response.status_code == 422
