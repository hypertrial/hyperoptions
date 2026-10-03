"""Boundaries and response isolation for live IV calculation details."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import options_api.main as main
from options_api.greeks import compute_greeks, years_until_expiry_close
from options_api.iv_diagnostics import iv_details_for_contract
from options_api.market_sources import DividendStatus, TreasuryCurve
from options_api.models import TickerListing
from options_api.pricing_context import EntryQuote, PricingContext, build_pricing_context

from .synthetic import NOW, TODAY
from .test_quant_api import _ChainFeed, _app


@pytest.mark.parametrize("side", ["call", "put"])
@pytest.mark.parametrize(("midpoint", "available"), [
    (Decimal("0.010000"), False),
    (Decimal("0.010001"), True),
])
def test_one_cent_midpoint_boundary_does_not_gate_endpoints(side, midpoint, available):
    expiry = TODAY + timedelta(days=7)
    strike = Decimal(100)
    quote = EntryQuote(
        spot=strike, stock_ask=strike, bid=midpoint - Decimal("0.005"),
        ask=midpoint + Decimal("0.005"), session_date=TODAY, source="nasdaq",
        fetched_at=NOW, rate=Decimal(0), valuation_time=NOW, rate_as_of_session=TODAY,
        spot_basis="underlying_midpoint", underlying_quote_time=NOW,
    )
    years = years_until_expiry_close(expiry, NOW)
    greeks = compute_greeks(
        side, quote.spot, strike, 7, quote.rate, quote.bid, quote.ask,
        years_to_expiry=years,
    )
    details = iv_details_for_contract(
        side=side, expiry=expiry, strike=strike, quote=quote, greeks=greeks,
        issue=None, pricing_path="displayed_chain",
    )

    assert (greeks.iv_pct_tenths is not None) is available
    assert details.status == ("available" if available else "unavailable")
    if available:
        assert details.reason is None
    else:
        assert details.reason.code == "midpoint_eligibility"
    assert details.bid_pct_tenths is not None and details.bid_reason is None
    assert details.ask_pct_tenths is not None and details.ask_reason is None
    assert Decimal(details.mid_price_exact) == midpoint


@pytest.mark.parametrize(("side", "path"), [
    ("call", "/api/covered-calls/IREN?moneyness=otm"),
    ("put", "/api/cash-secured-puts/IREN?moneyness=itm"),
])
def test_live_responses_refresh_diagnostics_without_changing_baseline_memo(
    tmp_path, monkeypatch: pytest.MonkeyPatch, side, path,
):
    feed = _ChainFeed()
    feed.chain = feed.chain.model_copy(update={"rows": [feed.chain.rows[5]]})
    contexts = []
    for valuation, rate in ((NOW, .04), (NOW + timedelta(seconds=60), .06)):
        context = build_pricing_context(
            "IREN", feed.chain, feed.info, valuation, None,
            TreasuryCurve(TODAY, ((.01, rate), (1, rate), (2, rate))),
            DividendStatus("nonpayer", valuation),
        )
        assert isinstance(context, PricingContext)
        contexts.append(context)
    phase = {"index": 0}
    captured = []
    original_quant = main.quant_for_contract

    async def pricing_context(*_args):
        return contexts[phase["index"]]

    def capture_quant(*args, **kwargs):
        result = original_quant(*args, **kwargs)
        captured.append(result.iv_details)
        return result

    monkeypatch.setattr(main, "quant_for_contract", capture_quant)
    app = _app(tmp_path, lambda: NOW)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
        monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
        monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
        monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
        monkeypatch.setattr(app.state.market_odds, "pricing_context_for", pricing_context)
        monkeypatch.setattr(app.state.market_odds, "schedule", lambda *_args: None)
        monkeypatch.setattr(app.state.market_odds, "schedule_for_chain", lambda *_args: False)
        monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda *_args: None)

        first_response = client.get(path)
        assert first_response.status_code == 200
        first = first_response.json()["expirations"][0]["contracts"][0]["iv_details"]
        first_details = captured[0]
        first_facts = first_details.model_dump(mode="json")
        memo_entry = app.state.service.memo._entries[("IREN", side)]
        baseline = next(iter(memo_entry.contracts.values()))
        assert baseline.iv_details is None

        phase["index"] = 1
        second_response = client.get(path)
        assert second_response.status_code == 200
        second = second_response.json()["expirations"][0]["contracts"][0]["iv_details"]
        second_details = captured[1]

        # The same rows, page clock, default rate, and memoized contracts were reused.
        assert app.state.service.memo._entries[("IREN", side)] is memo_entry
        assert next(iter(memo_entry.contracts.values())) is baseline
        assert baseline.iv_details is None
        assert first_details is not second_details
        assert first_details.model_dump(mode="json") == first_facts == first
        assert first["status"] == second["status"] == "available"
        assert first["rate_exact"] == "0.04" and second["rate_exact"] == "0.06"
        assert first["valuation_time"] == NOW.isoformat().replace("+00:00", "Z")
        second_time = (NOW + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
        assert second["valuation_time"] == second_time
        assert first["years_to_expiry_exact"] != second["years_to_expiry_exact"]
        for field in ("spot_exact", "bid_price_exact", "ask_price_exact",
                      "underlying_quote_time", "option_chain_fetched_at"):
            assert first[field] == second[field]
        assert date.fromisoformat(second["quote_session_date"]) == TODAY
