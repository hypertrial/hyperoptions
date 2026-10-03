from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from .conftest import load_fixture
from options_api.main import create_app
from options_api.models import TickerListing
from options_api.pricing_context import pricing_issue
from stocksweeper.config import Settings


@pytest.mark.parametrize("expiry,close", [
    ("2026-09-18", datetime(2026, 9, 18, 20, tzinfo=UTC)),
    ("2026-11-27", datetime(2026, 11, 27, 18, tzinfo=UTC)),
])
@pytest.mark.parametrize("action", ["covered-calls", "cash-secured-puts", "watch"])
@pytest.mark.parametrize("seconds_after", [-1, 0, 1])
def test_expiry_checked_after_acquisition(tmp_path, expiry, close, action, seconds_after):
    before = close - timedelta(seconds=2)
    after = close + timedelta(seconds=seconds_after)
    crossed = seconds_after >= 0
    clock = [before]
    payload = load_fixture("nasdaq_iren_sample.json")
    for row in payload["data"]["table"]["rows"]:
        if row.get("expirygroup"):
            row["expirygroup"] = datetime.fromisoformat(expiry).strftime("%B %d, %Y")
        if row.get("drillDownURL"):
            row["drillDownURL"] = row["drillDownURL"].replace(
                "260918", datetime.fromisoformat(expiry).strftime("%y%m%d")
            )
        if row.get("expiryDate"):
            row["expiryDate"] = datetime.fromisoformat(expiry).strftime("%b %d")

    def handler(request):
        url = str(request.url)
        if "option-chain" in url:
            clock[0] = after
            return httpx.Response(200, json=payload)
        if "/info" in url:
            return httpx.Response(200, json={"status": {"rCode": 200}, "data": {}})
        if "/historical" in url:
            return httpx.Response(200, json={
                "status": {"rCode": 200}, "data": {"tradesTable": {"rows": []}}
            })
        raise AssertionError(url)

    app = create_app(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: clock[0], prefetch_universe=False, predictive_refresh=False,
        research_settings=Settings(data_dir=tmp_path),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
        app.state.market_odds.schedule = lambda _: None
        app.state.market_odds.schedule_for_chain = lambda *args: False
        app.state.predictive_odds.schedule = lambda _: None

        async def no_external_pricing(*args):
            return pricing_issue("Contract terms cannot be verified")

        app.state.market_odds.pricing_context_for = no_external_pricing
        if action == "watch":
            response = client.post("/api/watchlist", json={
                "watch_key": f"w1:IREN:IREN:call:{expiry}:48.000"
            }, headers={"Origin": "http://localhost:5173"})
            assert response.status_code == (409 if crossed else 200), response.text
            records = app.state.watchlist.store.list()
            assert len(records) == (0 if crossed else 1)
            if records:
                assert records[0].created_at == after
        else:
            response = client.get(f"/api/{action}/IREN?moneyness=otm")
            assert response.status_code == 200, response.text
            groups = response.json()["expirations"]
            assert bool(groups) is not crossed
            if groups:
                assert groups[0]["expiration"] == expiry


@pytest.mark.parametrize("side", ["call", "put"])
@pytest.mark.asyncio
async def test_loader_clock_updates_new_york_date_but_fixed_now_stays_deterministic(
    monkeypatch, side
):
    from types import SimpleNamespace

    from options_api.chain import load_cash_secured_puts, load_covered_calls
    from options_api.memo import ContractMemo

    from .test_itm_calls import _chain, _history, _info, _put_quote, _quote

    before = datetime(2026, 9, 18, 3, 59, 59, tzinfo=UTC)
    after = before + timedelta(seconds=2)
    quote = _quote("2026-09-21", 45) if side == "call" else _put_quote("2026-09-21", 55)
    context = (_chain(quote), _info(), _history())

    async def acquire(*args):
        return context

    monkeypatch.setattr("options_api.chain._load_context", acquire)
    service = SimpleNamespace(memo=ContractMemo())
    load = load_covered_calls if side == "call" else load_cash_secured_puts
    fixed = await load(service, "IREN", before, moneyness="itm")
    live = await load(service, "IREN", before, moneyness="itm", clock=lambda: after)
    assert fixed.page.fetched_at == before
    assert fixed.page.expirations[0].dte == 4
    assert live.page.fetched_at == after
    assert live.page.expirations[0].dte == 3
    assert live.page.chain_fetched_at == context[0].fetched_at
