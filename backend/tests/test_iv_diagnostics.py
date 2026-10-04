"""IV quote sensitivity, provenance, and fail-closed page pricing."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from options_api.chain import assemble_cash_secured_puts, assemble_covered_calls
from options_api.greeks import (
    SIGMA_HI, black_scholes_price, compute_greeks, empty_greeks, endpoint_iv,
)
from options_api.iv_diagnostics import iv_details_for_contract
from options_api.live_quant import quant_for_contract
from options_api.market_calendar import session_close
from options_api.market_sources import DividendStatus, TreasuryCurve
from options_api.market_watch import MarketWatchOdds, _Snapshot
from options_api.memo import ContractMemo
from options_api.pricing_context import (
    PricingContext, PricingIssue, PricingSelection, build_pricing_context, pricing_issue,
)

from .synthetic import NOW, TODAY, synthetic_context
from .test_live_quant import EXPIRY, STRIKE, _Market, _Predictive, _distribution, _quote


def _curve(day=TODAY):
    return TreasuryCurve(day, ((0.01, 0.04), (1, 0.04), (2, 0.04)))


@pytest.mark.parametrize("side", ["call", "put"])
def test_endpoint_round_trip_bounds_and_ceiling(side):
    spot, strike, years, rate = map(Decimal, ("100", "100", "1", "0.05"))
    price = Decimal(str(black_scholes_price(side == "call", 100, 100, 1, 0.05, 0.2)))
    assert endpoint_iv(side, spot, strike, years, rate, price) == (200, None)
    discounted = strike * (-rate * years).exp()
    lower = max(Decimal(0), spot - discounted if side == "call" else discounted - spot)
    upper = spot if side == "call" else discounted
    for price in (Decimal(0), lower, upper, upper + Decimal(".01"), Decimal("NaN")):
        iv, reason = endpoint_iv(side, spot, strike, years, rate, price)
        assert iv is None and reason.code == "model_bounds"
    high = Decimal(str(black_scholes_price(side == "call", 100, 100, 1, .05, SIGMA_HI)))
    assert endpoint_iv(side, spot, strike, years, rate, high) == (5000, None)
    iv, reason = endpoint_iv(side, spot, strike, years, rate, high + Decimal(".01"))
    assert iv is None and reason.code == "solver_range"
    if side == "put":
        assert endpoint_iv(side, spot, strike, years, rate, Decimal(98))[1].code == "model_bounds"
    iv, reason = endpoint_iv(side, spot, strike, years, Decimal(0), Decimal(".009"))
    assert iv is not None and reason is None


@pytest.mark.parametrize("root", [5.5, .4, float("nan")])
def test_bad_root_is_not_clamped(monkeypatch, root):
    monkeypatch.setattr("options_api.greeks.implied_vol", lambda *_args: root)
    iv, reason = endpoint_iv("call", Decimal(100), Decimal(100), Decimal(1),
                             Decimal(".05"), Decimal("10.45"))
    assert iv is None and reason.code == "numerical_failure"


def test_endpoints_survive_midpoint_buffer_and_zero_bid():
    quote = replace(_quote(), bid=Decimal(0), ask=Decimal(".018"), rate=Decimal(0))
    years = Decimal(str((session_close(EXPIRY) - quote.valuation_time).total_seconds())) / 31536000
    greeks = compute_greeks("call", quote.spot, STRIKE, 7, quote.rate, quote.bid, quote.ask,
                            years_to_expiry=years)
    details = iv_details_for_contract(side="call", expiry=EXPIRY, strike=STRIKE, quote=quote,
                                     greeks=greeks, issue=None, pricing_path="displayed_chain")
    assert greeks.iv_pct_tenths is None and details.status == "unavailable"
    assert details.reason.code == "midpoint_eligibility"
    assert details.bid_pct_tenths is None and details.bid_reason.code == "model_bounds"
    assert details.ask_pct_tenths is not None and details.ask_reason is None


def _context(changes=None, dividends=None, curve=None):
    chain, info, _history, _today, now = synthetic_context()
    row = chain.rows[5].model_copy(update={"root": "IREN", **(changes or {})})
    chain = chain.model_copy(update={"rows": [row]})
    result = build_pricing_context("IREN", chain, info, now, None, curve or _curve(),
                                   dividends or DividendStatus("nonpayer", now))
    assert isinstance(result, PricingContext)
    return result


@pytest.mark.parametrize(("changes", "message"), [
    ({"call_bid": None}, "missing or non-finite"),
    ({"call_bid": Decimal("NaN")}, "missing or non-finite"),
    ({"call_bid": Decimal(5), "call_ask": Decimal(4)}, "invalid or crossed"),
    ({"call_bid": Decimal(2), "call_ask": Decimal(2)}, "Locked"),
    ({"call_bid": Decimal(49), "call_ask": Decimal(50)}, "pricing cap"),
    ({"call_volume": 0, "call_open_interest": 0, "call_bid": Decimal(1),
      "call_ask": Decimal(5)}, "insufficient trading activity"),
    ({"call_bid": Decimal(1), "call_ask": Decimal(5)}, "spread is too wide"),
])
def test_quote_rejection_precedence_and_side_isolation(changes, message):
    context = _context(changes)
    selection = context.selection("call", "2026-09-25", Decimal(50))
    assert selection.quote is None and selection.issue.code == "option_quote"
    assert message in selection.issue.message
    assert context.selection("put", "2026-09-25", Decimal(50)).quote is not None


@pytest.mark.parametrize(("dividends", "curve", "code"), [
    (DividendStatus("payer", NOW), None, "dividends"),
    (DividendStatus("unknown", NOW), None, "dividends"),
    (DividendStatus("payer", NOW), _curve(date(2026, 8, 1)), "treasury"),
])
def test_expiry_issue_preserves_entry_risk_and_gates_all_iv(dividends, curve, code):
    selection = _context(dividends=dividends, curve=curve).selection(
        "put", "2026-09-25", Decimal(50))
    assert selection.quote is not None and selection.issue.code == code
    selection = PricingSelection(replace(_quote(), rate=None), selection.issue)
    result = quant_for_contract(_Market(None), _Predictive(_distribution()), ticker="IREN",
                                root="IREN", side="put", expiry=EXPIRY, strike=STRIKE,
                                pricing_selection=selection, include_iv_details=True)
    assert result.risk.status == "available" and result.greeks.iv_pct_tenths is None
    assert result.iv_details.reason.code == code
    assert result.iv_details.bid_pct_tenths is result.iv_details.ask_pct_tenths is None
    assert result.iv_details.bid_reason.code == result.iv_details.ask_reason.code == code


def test_exact_provenance_and_early_close():
    quote = replace(_quote(), spot=Decimal("100.000123400"), bid=Decimal("5.00010"),
                    ask=Decimal("5.10030"), rate=Decimal(".04001230"),
                    spot_basis="underlying_midpoint", underlying_quote_time=_quote().valuation_time)
    result = quant_for_contract(_Market(None), _Predictive(_distribution()), ticker="IREN",
                                root="IREN", side="call", expiry=EXPIRY, strike=STRIKE,
                                pricing_selection=PricingSelection(quote), include_iv_details=True,
                                iv_pricing_path="matching_snapshot")
    details = result.iv_details
    assert details.status == "available" and details.reason is None
    assert details.spot_exact == "100.000123400" and details.mid_price_exact == "5.05020"
    assert details.bid_price_exact == "5.00010" and details.ask_price_exact == "5.10030"
    assert details.rate_exact == "0.04001230"
    assert details.valuation_time == quote.valuation_time
    assert details.underlying_quote_time == quote.underlying_quote_time
    assert details.option_chain_fetched_at == quote.fetched_at
    assert details.pricing_path == "matching_snapshot"
    early = date(2026, 11, 27)
    early_quote = replace(quote, valuation_time=datetime(2026, 11, 26, 18, tzinfo=UTC))
    early_details = iv_details_for_contract(side="put", expiry=early, strike=STRIKE,
                                          quote=early_quote, greeks=empty_greeks(), issue=None,
                                          pricing_path="displayed_chain")
    assert early_details.expiry_close == datetime(2026, 11, 27, 18, tzinfo=UTC)
    assert early_details.years_to_expiry_exact == format(Decimal(86400) / Decimal(31536000), "f")


def test_spot_bases():
    chain, info, _history, _today, now = synthetic_context()
    dividends = DividendStatus("nonpayer", now)
    nasdaq = build_pricing_context("IREN", chain, info, now, None, _curve(), dividends)
    assert nasdaq.spot_basis == "underlying_midpoint"
    yahoo = chain.model_copy(update={"source": "yahoo", "spot": Decimal("51.00001"),
                                    "last_trade_timestamp": now.isoformat()})
    ycontext = build_pricing_context("IREN", yahoo, info, now, None, _curve(), dividends)
    assert ycontext.spot_basis == "yahoo_regular_market_price" and ycontext.spot == yahoo.spot
    closed = build_pricing_context("IREN", chain, info, now.replace(hour=21),
                                   Decimal("52.00002"), _curve(), dividends)
    assert closed.spot_basis == "completed_session_close" and closed.spot == Decimal("52.00002")
    assert closed.underlying_quote_time is None and closed.valuation_time == session_close(TODAY)


@pytest.mark.parametrize("assemble", [assemble_covered_calls, assemble_cash_secured_puts])
def test_baseline_memo_never_solves_endpoints_or_retains_live_details(monkeypatch, assemble):
    def unexpected(*_args):
        pytest.fail("baseline/memo must not solve endpoints")
    monkeypatch.setattr("options_api.iv_diagnostics.endpoint_iv", unexpected)
    chain, info, history, today, now = synthetic_context()
    memo = ContractMemo()
    first = assemble(chain, info, history, today, now, "all", memo=memo)
    first.expirations[0].contracts[0].iv_details = iv_details_for_contract(
        side="call", expiry=EXPIRY, strike=STRIKE, quote=None, greeks=empty_greeks(),
        issue=pricing_issue("Contract terms cannot be verified"), pricing_path="displayed_chain")
    second = assemble(chain, info, history, today, now, "all", memo=memo)
    assert all(c.iv_details is None for g in second.expirations for c in g.contracts)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["semantic", "build", "timeout", "acquisition"])
async def test_failure_type_controls_snapshot_fallback(monkeypatch, failure):
    chain, info, history, _today, now = synthetic_context()
    chain = chain.model_copy(update={"rows": [row.model_copy(update={"root": "IREN"})
                                               for row in chain.rows]})
    if failure == "semantic":
        chain = chain.model_copy(update={"truncated": True})
    async def inputs(*_args):
        if failure == "timeout":
            raise TimeoutError("private provider URL")
        if failure == "acquisition":
            raise OSError("private provider payload")
        return _curve(), DividendStatus("nonpayer", now), False
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(object(), client, lambda: now)
        monkeypatch.setattr(odds, "_page_curve_and_dividends", inputs)
        if failure == "build":
            def bad_context(*_args, **_kwargs):
                raise ValueError("private provider data")
            monkeypatch.setattr("options_api.market_watch.build_pricing_context", bad_context)
        result = await odds.pricing_context_for("IREN", chain, info, history, now)
        if failure in {"timeout", "acquisition"}:
            assert isinstance(result, PricingContext)
            issue = result.selection("call", "2026-09-25", Decimal(50)).issue
            assert issue.fallback_allowed and issue.code == "input_acquisition"
        else:
            assert isinstance(result, PricingIssue) and not result.fallback_allowed
            issue = result
        assert "private" not in issue.message
        await odds.close()


@pytest.mark.asyncio
async def test_fallback_requires_valid_matching_generation_and_retains_own_facts():
    chain, _info, _history, _today, now = synthetic_context()
    context = _context()
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(object(), client, lambda: now)
        snap = _Snapshot(chain.fetched_at, TODAY, "nasdaq", {}, context.reasons,
                         spot=context.spot, stock_ask=context.stock_ask,
                         valuation_time=now - timedelta(seconds=30),
                         underlying_quote_time=now - timedelta(seconds=45),
                         rate_as_of_session=TODAY, rates=context.rates,
                         entry_quotes=context.entry_quotes, valid_contracts=context.valid_contracts,
                         spot_basis=context.spot_basis, quote_issues=context.quote_issues)
        odds._cache["IREN"] = snap
        selection = odds.pricing_selection("IREN", "call", "2026-09-25", Decimal(50),
                                           chain.fetched_at, "nasdaq")
        assert selection.quote.valuation_time == snap.valuation_time
        assert selection.quote.underlying_quote_time == snap.underlying_quote_time
        assert odds.pricing_selection("IREN", "call", "2026-09-25", Decimal(50),
                                      chain.fetched_at + timedelta(seconds=1), "nasdaq") is None
        assert odds.pricing_selection("IREN", "call", "2026-09-25", Decimal(50),
                                      chain.fetched_at, "yahoo") is None
        snap.fetched_at = now - timedelta(minutes=6)
        assert odds.pricing_selection("IREN", "call", "2026-09-25", Decimal(50),
                                      snap.fetched_at, "nasdaq") is None
        await odds.close()


def test_midpoint_put_above_discounted_strike_reports_true_model_bound():
    quote = replace(_quote(), spot=Decimal(1), bid=Decimal(98), ask=Decimal("98.1"),
                    rate=Decimal(".05"), valuation_time=datetime(2025, 10, 2, 20, tzinfo=UTC))
    details = iv_details_for_contract(side="put", expiry=EXPIRY, strike=Decimal(100),
                                     quote=quote, greeks=empty_greeks(), issue=None,
                                     pricing_path="displayed_chain")
    assert details.reason.code == "model_bounds"


def test_failed_numerical_midpoint_diagnostic_returns_safe_reason(monkeypatch):
    def fail(*_args):
        raise OverflowError("private numerical input")
    monkeypatch.setattr("options_api.iv_diagnostics.black_scholes_price", fail)
    details = iv_details_for_contract(side="call", expiry=EXPIRY, strike=STRIKE,
                                     quote=_quote(), greeks=empty_greeks(), issue=None,
                                     pricing_path="displayed_chain")
    assert details.reason.code == "numerical_failure" and "private" not in details.reason.message


def test_live_midpoint_numerical_failure_preserves_risk(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OverflowError("private numerical input")
    monkeypatch.setattr("options_api.live_quant.compute_greeks", fail)
    result = quant_for_contract(_Market(None), _Predictive(_distribution()), ticker="IREN",
                                root="IREN", side="put", expiry=EXPIRY, strike=STRIKE,
                                pricing_selection=PricingSelection(_quote()),
                                include_iv_details=True)
    assert result.greeks.iv_pct_tenths is None and result.risk.status == "available"
    assert result.iv_details.reason.code == "numerical_failure"
    assert result.iv_details.bid_pct_tenths is not None
    assert result.iv_details.ask_pct_tenths is not None


def test_expiry_precedence_is_timing_then_treasury_then_dividends_then_quote():
    same = _context(changes={"expiration": TODAY.isoformat(), "call_bid": None},
                    dividends=DividendStatus("payer", NOW), curve=_curve(date(2026, 8, 1)))
    selection = same.selection("call", TODAY.isoformat(), Decimal(50))
    assert selection.issue.code == "expiry_timing"
    gated = _context(changes={"call_bid": None}, dividends=DividendStatus("payer", NOW),
                     curve=_curve(date(2026, 8, 1)))
    assert gated.selection("call", "2026-09-25", Decimal(50)).issue.code == "treasury"
    gated = _context(changes={"call_bid": None}, dividends=DividendStatus("payer", NOW))
    assert gated.selection("call", "2026-09-25", Decimal(50)).issue.code == "dividends"


def test_duplicate_identity_precedes_expiry_and_quote_issues():
    chain, info, _history, _today, now = synthetic_context()
    row = chain.rows[5].model_copy(update={"root": "IREN", "call_bid": None})
    chain = chain.model_copy(update={"rows": [row, row.model_copy()]})
    context = build_pricing_context("IREN", chain, info, now, None, _curve(date(2026, 8, 1)),
                                    DividendStatus("payer", now))
    selection = context.selection("call", row.expiration, row.strike)
    assert selection.quote is None and selection.issue.code == "contract_identity"
    assert "more than once" in selection.issue.message


@pytest.mark.parametrize(("changes", "info_changes", "message"), [
    ({"fetched_at": NOW - timedelta(days=1)}, {}, "another quote session"),
    ({"truncated": True}, {}, "coverage is incomplete"),
    ({}, {"is_real_time": False}, "not marked real time"),
    ({}, {"quote_timestamp": None}, "time or price is unavailable"),
    ({}, {"quote_timestamp": (NOW - timedelta(minutes=6)).isoformat()}, "is stale"),
    ({}, {"bid": Decimal("NaN")}, "bid and ask is unavailable"),
])
def test_chain_and_underlying_fixed_reasons(changes, info_changes, message):
    chain, info, _history, _today, now = synthetic_context()
    result = build_pricing_context("IREN", chain.model_copy(update=changes),
                                   info.model_copy(update=info_changes), now, None, _curve(),
                                   DividendStatus("nonpayer", now))
    assert isinstance(result, str) and message in result
    assert pricing_issue(result).code in {"chain_session", "underlying"}


@pytest.mark.parametrize(("code", "allow", "matching"), [
    ("underlying", False, True),
    ("numerical_failure", False, True),
    ("input_acquisition", True, True),
    ("input_acquisition", True, False),
])
def test_api_fallback_and_whole_response_diagnostics(tmp_path, monkeypatch, code, allow, matching):
    from fastapi.testclient import TestClient
    from options_api.models import TickerListing
    from .test_quant_api import _ChainFeed, _app

    app = _app(tmp_path, lambda: NOW)
    feed = _ChainFeed()
    context = build_pricing_context("IREN", feed.chain, feed.info, NOW, None, _curve(),
                                    DividendStatus("nonpayer", NOW))
    async def failed(*_args):
        return PricingIssue(code, "Fixed test acquisition reason", allow)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
        monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
        monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
        monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
        monkeypatch.setattr(app.state.market_odds, "pricing_context_for", failed)
        monkeypatch.setattr(app.state.market_odds, "schedule", lambda *_args: None)
        monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda *_args: None)
        stamp = feed.chain.fetched_at if matching else feed.chain.fetched_at - timedelta(seconds=1)
        app.state.market_odds._cache["IREN"] = _Snapshot(
            stamp, TODAY, "nasdaq", {}, {}, spot=context.spot, stock_ask=context.stock_ask,
            valuation_time=NOW - timedelta(seconds=30), underlying_quote_time=NOW,
            rate_as_of_session=TODAY, rates=context.rates, entry_quotes=context.entry_quotes,
            valid_contracts=context.valid_contracts, spot_basis=context.spot_basis,
            quote_issues=context.quote_issues)
        for url in ("/api/covered-calls/IREN", "/api/cash-secured-puts/IREN"):
            response = client.get(url)
            assert response.status_code == 200
            rows = [row for group in response.json()["expirations"] for row in group["contracts"]]
            assert all(row["iv_details"] is not None for row in rows)
            available = [row for row in rows if row["iv_pct_tenths"] is not None]
            if allow and matching:
                assert available
                for row in available:
                    details = row["iv_details"]
                    assert details["status"] == "available" and details["reason"] is None
                    assert details["pricing_path"] == "matching_snapshot"
                    stamp = (NOW - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
                    assert details["valuation_time"] == stamp
            else:
                assert not available
                assert all(row["iv_details"]["reason"]["code"] == code for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_failure", [True, False])
async def test_actual_treasury_acquisition_is_distinct_from_successful_invalid_data(
    monkeypatch, transport_failure,
):
    import options_api.market_sources as sources
    chain, info, history, _today, now = synthetic_context()
    chain = chain.model_copy(update={"rows": [row.model_copy(update={"root": "IREN"})
                                               for row in chain.rows]})
    calls = []
    def transport(request):
        calls.append(request.url)
        if transport_failure:
            raise httpx.ConnectError("private upstream URL", request=request)
        return httpx.Response(200, content=b"<feed/>")
    async def dividend(*_args):
        return DividendStatus("nonpayer", now)
    monkeypatch.setattr(sources, "_treasury_cache", None)
    monkeypatch.setattr("options_api.market_watch.fetch_dividend_status", dividend)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        odds = MarketWatchOdds(object(), client, lambda: now)
        result = await odds.pricing_context_for("IREN", chain, info, history, now)
        assert len(calls) == 2
        assert isinstance(result, PricingContext)
        if transport_failure:
            issue = result.selection("call", "2026-09-25", Decimal(50)).issue
            assert issue.fallback_allowed and issue.code == "input_acquisition"
            assert "private" not in issue.message
        else:
            assert pricing_issue(result.reasons["2026-09-25"]).code == "treasury"
        await odds.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_failure", [True, False])
async def test_actual_dividend_acquisition_is_distinct_from_semantic_unknown(
    monkeypatch, transport_failure,
):
    import options_api.market_sources as sources
    chain, info, history, _today, now = synthetic_context()
    chain = chain.model_copy(update={"rows": [row.model_copy(update={"root": "IREN"})
                                               for row in chain.rows]})
    def dividend_sync(*_args):
        if transport_failure:
            raise OSError("private upstream dividend payload")
        return DividendStatus("unknown", now)
    async def treasury(*_args, **_kwargs):
        return _curve()
    monkeypatch.setattr(sources, "_dividend_status_sync", dividend_sync)
    monkeypatch.setattr("options_api.market_watch.fetch_treasury_curve", treasury)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(object(), client, lambda: now)
        result = await odds.pricing_context_for("IREN", chain, info, history, now)
        assert isinstance(result, PricingContext)
        if transport_failure:
            issue = result.selection("call", "2026-09-25", Decimal(50)).issue
            assert issue.fallback_allowed and issue.code == "input_acquisition"
            assert "private" not in issue.message
        else:
            assert pricing_issue(result.reasons["2026-09-25"]).code == "dividends"
        await odds.close()


@pytest.mark.asyncio
async def test_dividend_worker_saturation_retains_acquisition_evidence():
    from options_api.market_sources import _DIVIDEND_WORKERS, fetch_dividend_status
    for _ in range(4):
        assert _DIVIDEND_WORKERS.acquire(blocking=False)
    try:
        status = await fetch_dividend_status("IREN", NOW)
        assert status.kind == "unknown" and status.acquisition_failed
    finally:
        for _ in range(4):
            _DIVIDEND_WORKERS.release()


@pytest.mark.asyncio
@pytest.mark.parametrize(("curve_kind", "dividend_kind", "same_day", "invalid_quote", "code"), [
    ("invalid", "failed", False, False, "treasury"),
    ("failed", "payer", False, False, "dividends"),
    ("failed", "unknown", False, False, "dividends"),
    ("slow", "payer", False, False, "dividends"),
    ("invalid", "slow", False, False, "treasury"),
    ("valid", "slow", False, False, "input_pending"),
    ("slow", "slow", True, False, "expiry_timing"),
    ("failed", "nonpayer", False, True, "option_quote"),
    ("programming_error", "slow", False, False, "numerical_failure"),
])
async def test_mixed_input_semantics_survive_failures_and_timeout(
    monkeypatch, curve_kind, dividend_kind, same_day, invalid_quote, code,
):
    import asyncio
    from options_api.market_sources import InputAcquisitionError
    chain, info, history, _today, now = synthetic_context()
    expiry = TODAY.isoformat() if same_day else "2026-09-25"
    row = chain.rows[5].model_copy(update={"root": "IREN", "expiration": expiry,
                                         **({"call_bid": None} if invalid_quote else {})})
    chain = chain.model_copy(update={"rows": [row]})
    async def treasury(*_args, **_kwargs):
        if curve_kind == "slow":
            await asyncio.sleep(1)
        if curve_kind == "failed":
            raise InputAcquisitionError("private upstream")
        if curve_kind == "programming_error":
            raise RuntimeError("private programming exception")
        return None if curve_kind == "invalid" else _curve()
    async def dividend(*_args):
        if dividend_kind == "slow":
            await asyncio.sleep(1)
        return DividendStatus(
            "unknown" if dividend_kind == "failed" else dividend_kind, now,
            acquisition_failed=dividend_kind == "failed",
        )
    monkeypatch.setattr("options_api.market_watch.PAGE_INPUT_TIMEOUT", .02)
    monkeypatch.setattr("options_api.market_watch.fetch_treasury_curve", treasury)
    monkeypatch.setattr("options_api.market_watch.fetch_dividend_status", dividend)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(object(), client, lambda: now)
        result = await odds.pricing_context_for("IREN", chain, info, history, now)
        if isinstance(result, PricingContext):
            selection = result.selection("call", expiry, Decimal(50))
            issue = selection.issue
            assert selection.quote is not None or invalid_quote
        else:
            issue = result
        assert issue.code == code
        assert issue.fallback_allowed == (code in {"input_acquisition", "input_pending"})
        assert "private" not in issue.message
        if code == "input_pending":
            assert isinstance(result, PricingContext)
            assert selection.quote is not None and selection.quote.rate is None
            assert selection.display_rate is not None
            quantified = quant_for_contract(
                _Market(None), _Predictive(_distribution()), ticker="IREN", root="IREN",
                side="call", expiry=date.fromisoformat(expiry), strike=Decimal(50),
                pricing_selection=selection, include_iv_details=True,
            )
            assert quantified.greeks.iv_pct_tenths is None
            assert quantified.iv_details.status == "pending"
            assert quantified.iv_details.rate_exact == format(selection.display_rate, "f")
        await odds.close()


@pytest.mark.asyncio
async def test_pending_dividend_resolves_to_iv_on_the_next_read(monkeypatch):
    import asyncio
    chain, info, history, _today, now = synthetic_context()
    row = chain.rows[5].model_copy(update={"root": "IREN"})
    chain = chain.model_copy(update={"rows": [row]})
    release = asyncio.Event()

    async def treasury(*_args, **_kwargs):
        return _curve()

    async def dividend(*_args):
        await release.wait()
        return DividendStatus("nonpayer", now)

    monkeypatch.setattr("options_api.market_watch.PAGE_INPUT_TIMEOUT", 0.02)
    monkeypatch.setattr("options_api.market_watch.fetch_treasury_curve", treasury)
    monkeypatch.setattr("options_api.market_watch.fetch_dividend_status", dividend)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(object(), client, lambda: now)
        first = await odds.pricing_context_for("IREN", chain, info, history, now)
        assert isinstance(first, PricingContext)
        assert first.selection("call", "2026-09-25", Decimal(50)).issue.code == "input_pending"
        flight = odds._page_input_flights.get("IREN")
        assert flight is not None
        release.set()
        await flight
        second = await odds.pricing_context_for("IREN", chain, info, history, now)
        assert isinstance(second, PricingContext)
        selection = second.selection("call", "2026-09-25", Decimal(50))
        assert selection.issue is None and selection.quote is not None
        assert selection.quote.rate is not None
        quantified = quant_for_contract(
            _Market(None), _Predictive(_distribution()), ticker="IREN", root="IREN",
            side="call", expiry=date(2026, 9, 25), strike=Decimal(50),
            pricing_selection=selection, include_iv_details=True,
        )
        assert quantified.greeks.iv_pct_tenths is not None
        assert quantified.iv_details.status == "available"
        await odds.close()


@pytest.mark.asyncio
async def test_treasury_failure_cooldown_skips_the_network():
    import options_api.market_sources as sources
    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    now = datetime(2026, 10, 2, 16, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        assert await sources.fetch_treasury_curve(client, now) is None
    assert calls["n"] == 2
    with pytest.raises(sources.InputAcquisitionError):
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await sources.fetch_treasury_curve(
                client, now + timedelta(minutes=4), raise_on_acquisition_failure=True
            )
    assert calls["n"] == 2
    cached = _curve(date(2026, 10, 2))
    sources._treasury_cache = (now - timedelta(hours=3), cached)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        assert await sources.fetch_treasury_curve(client, now + timedelta(minutes=1)) == cached
    assert calls["n"] == 2
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        assert await sources.fetch_treasury_curve(client, now + timedelta(minutes=6)) == cached
    assert calls["n"] == 4


@pytest.mark.parametrize("invalid", ["dividends", "rate", "rate_date", "missing_quote"])
@pytest.mark.parametrize("current_has_quote", [False, True])
def test_matching_ineligible_snapshot_retains_current_acquisition_details(
    tmp_path, monkeypatch, invalid, current_has_quote,
):
    from fastapi.testclient import TestClient
    from options_api.models import TickerListing
    from .test_quant_api import _ChainFeed, _app

    app = _app(tmp_path, lambda: NOW)
    feed = _ChainFeed()
    context = build_pricing_context("IREN", feed.chain, feed.info, NOW, None, _curve(),
                                    DividendStatus("nonpayer", NOW))
    async def failed(*_args):
        if current_has_quote:
            return replace(context, rates={}, acquisition_expirations=set(context.rates))
        return PricingIssue("input_acquisition", "Current acquisition failure", True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
        monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
        monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
        monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
        monkeypatch.setattr(app.state.market_odds, "pricing_context_for", failed)
        monkeypatch.setattr(app.state.market_odds, "schedule", lambda *_args: None)
        monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda *_args: None)
        reasons = ({expiry: "Dividend status could not be verified" for expiry in context.rates}
                   if invalid == "dividends" else {})
        snapshot = _Snapshot(
            feed.chain.fetched_at, TODAY, "nasdaq", {}, reasons, spot=context.spot,
            stock_ask=context.stock_ask, valuation_time=NOW, underlying_quote_time=NOW,
            rate_as_of_session=(TODAY - timedelta(days=8) if invalid == "rate_date" else TODAY),
            rates={} if invalid == "rate" else context.rates,
            entry_quotes={} if invalid == "missing_quote" else context.entry_quotes,
            valid_contracts=context.valid_contracts, spot_basis=context.spot_basis,
            quote_issues=context.quote_issues)
        app.state.market_odds._cache["IREN"] = snapshot
        for url in ("/api/covered-calls/IREN", "/api/cash-secured-puts/IREN"):
            response = client.get(url)
            assert response.status_code == 200
            rows = [row for group in response.json()["expirations"] for row in group["contracts"]]
            assert rows
            for row in rows:
                details = row["iv_details"]
                assert row["iv_pct_tenths"] is None
                assert details["reason"]["code"] == "input_acquisition"
                assert details["rate_exact"] is None
                if current_has_quote and details["spot_exact"] is not None:
                    assert details["spot_exact"] == format(context.spot, "f")
                    assert details["pricing_path"] == "displayed_chain"
                    assert details["valuation_time"] == NOW.isoformat().replace("+00:00", "Z")
                else:
                    if not current_has_quote:
                        assert details["reason"]["message"] == "Current acquisition failure"
                    assert details["pricing_path"] is None and details["spot_exact"] is None
                    assert details["valuation_time"] is None


@pytest.mark.asyncio
async def test_snapshot_of_listed_holiday_expiry_cannot_fallback_after_mapped_session_close():
    # Good Friday maps to Thursday's completed expiry session.
    expiry = "2026-04-03"
    thursday = date(2026, 4, 2)
    valuation = session_close(thursday)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(object(), client, lambda: valuation + timedelta(minutes=1))
        odds._cache["IREN"] = _Snapshot(
            valuation, thursday, "nasdaq", {}, {}, spot=Decimal(100),
            stock_ask=Decimal("100.01"), valuation_time=valuation,
            rate_as_of_session=thursday, rates={expiry: Decimal(".04")},
            entry_quotes={("call", expiry, Decimal(100)): (Decimal(1), Decimal("1.1"))},
            valid_contracts={(expiry, Decimal(100))}, spot_basis="completed_session_close")
        assert odds.pricing_selection("IREN", "call", expiry, Decimal(100),
                                      valuation, "nasdaq") is None
        await odds.close()
