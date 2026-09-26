"""Adversarial tests of the market/predictive/risk coordination seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from math import sqrt
from types import SimpleNamespace

import pytest

from options_api.live_quant import quant_for_contract
from options_api.market_calendar import remaining_session_variance_fraction, session_close
from options_api.market_watch import EntryQuote
from options_api.models import MarketOddsView, PredictiveOddsView
from options_api.watchlist import OutcomeView, WatchItem, get_watchlist
from stocksweeper.forecast.predictive import PredictiveDistribution


EXPIRY = date(2026, 10, 2)
STRIKE = Decimal("100")


def _distribution(
    *, as_of: date = date(2026, 9, 25), expiry: date = EXPIRY
) -> PredictiveDistribution:
    return PredictiveDistribution(
        ticker="IREN", status="available", reason=None, method="empirical_scaled",
        as_of=as_of, expiry_session=expiry, horizon_sessions=5,
        spot=100.0, daily_volatility=0.02, model_version="test-v1", support=3,
        data_hash="1234", terminal_prices=(80.0, 100.0, 120.0),
        weights=(0.2, 0.5, 0.3),
    )


class _Market:
    def __init__(
        self, quote: EntryQuote | None,
        reason: str = "Quoted prices do not bound these odds narrowly enough",
    ) -> None:
        self.quote = quote
        self.reason = reason
        self.lookups: list[tuple[object, ...]] = []

    def lookup(self, *identity: object) -> MarketOddsView:
        self.lookups.append(identity)
        return MarketOddsView(
            status="unavailable", reason=self.reason,
            source="nasdaq", session_date=date(2026, 9, 25),
        )

    def lookup_last_good(self, *_identity: object) -> MarketOddsView:
        return MarketOddsView(
            status="available", itm_pct_tenths=400, otm_pct_tenths=600,
            session_date=date(2026, 9, 25), source="nasdaq",
        )

    def entry_quote(self, *_identity: object) -> EntryQuote | None:
        return self.quote

    def schedule(self, tickers: object) -> None:
        self.scheduled = list(tickers)


class _Predictive:
    def __init__(self, distribution: PredictiveDistribution) -> None:
        self.distribution = distribution
        self.calls: list[dict[str, object]] = []

    def lookup(self, _ticker: str, side: str, _expiry: date, _strike: Decimal,
               **kwargs: object):
        self.calls.append({"side": side, **kwargs})
        return PredictiveOddsView(
            status="available", method="empirical_scaled", itm_pct_tenths=300,
            otm_pct_tenths=200, atm_pct_tenths=500,
            as_of_session=self.distribution.as_of,
            expiry_session=self.distribution.expiry_session,
            model_version="test-v1", support=3, data_hash="1234",
        ), self.distribution

    def schedule(self, tickers: object) -> None:
        self.scheduled = list(tickers)


def _quote(
    *, session: date = date(2026, 9, 25),
    valuation: datetime = datetime(2026, 9, 25, 20, tzinfo=UTC),
) -> EntryQuote:
    return EntryQuote(
        spot=Decimal("100"), stock_ask=Decimal("101"),
        bid=Decimal("5"), ask=Decimal("5.10"),
        session_date=session, source="nasdaq", fetched_at=valuation,
        rate=Decimal("0.04"), valuation_time=valuation,
        rate_as_of_session=session,
    )


def test_wide_market_bound_still_publishes_labeled_predictive_and_coherent_risk() -> None:
    market = _Market(_quote())
    predictive = _Predictive(_distribution())

    call = quant_for_contract(
        market, predictive, ticker="IREN", root="IREN", side="call",
        expiry=EXPIRY, strike=STRIKE, watched=True, contract_since=date(2026, 9, 25),
    )
    put = quant_for_contract(
        market, predictive, ticker="IREN", root="IREN", side="put",
        expiry=EXPIRY, strike=STRIKE,
    )

    assert call.market.status == "unavailable"
    assert "bound" in (call.market.reason or "")
    assert call.predictive.status == "available"
    assert call.predictive.method == "empirical_scaled"
    assert call.last_available_market is not None
    assert call.last_available_market.session_date == date(2026, 9, 25)
    assert put.last_available_market is None
    assert call.risk.status == put.risk.status == "available"
    assert call.risk.assumed_spot_cents == 10_100  # Buy stock at ask.
    assert put.risk.assumed_spot_cents == 10_000  # Put has no stock purchase.
    assert call.risk.quote_session == put.risk.quote_session == date(2026, 9, 25)
    assert predictive.calls == [
        {"side": "call", "contract_since": date(2026, 9, 25)},
        {"side": "put", "contract_since": None},
    ]


def test_missing_entry_quote_withholds_risk_without_erasing_predictive_odds() -> None:
    result = quant_for_contract(
        _Market(None), _Predictive(_distribution()),
        ticker="IREN", root="IREN", side="call", expiry=EXPIRY, strike=STRIKE,
    )
    assert result.market.status == "unavailable"
    assert result.predictive.status == "available"
    assert result.risk.status == "unavailable"
    assert result.risk.expected_pnl_cents is None
    assert result.greeks.source is None


def test_greek_rate_uses_actual_treasury_curve_date() -> None:
    quote = replace(_quote(), rate_as_of_session=date(2026, 9, 23))
    result = quant_for_contract(
        _Market(quote), _Predictive(_distribution()),
        ticker="IREN", root="IREN", side="call", expiry=EXPIRY, strike=STRIKE,
    )
    assert result.greeks.source == "mid"
    assert result.greeks_rate_as_of_session == date(2026, 9, 23)


@pytest.mark.parametrize("reason", [
    "Dated Treasury rate is unavailable", "Dividend exposure is unsupported",
])
def test_market_gate_withholds_greeks_but_not_hypothetical_entry_risk(reason: str) -> None:
    market = _Market(replace(_quote(), rate=None), reason=reason)
    predictive = _Predictive(_distribution())
    for side in ("call", "put"):
        result = quant_for_contract(
            market, predictive, ticker="IREN", root="IREN", side=side,
            expiry=EXPIRY, strike=STRIKE,
        )
        assert result.market.status == "unavailable"
        assert result.market.reason == reason
        assert result.greeks.source is None
        assert result.greeks_rate_pct_tenths is None
        assert result.risk.status == "available"
        assert result.risk.expected_pnl_cents is not None
        assert result.risk.quote_session == date(2026, 9, 25)


def test_displayed_chain_cannot_borrow_entry_quote_from_another_snapshot() -> None:
    quote = _quote()
    result = quant_for_contract(
        _Market(quote), _Predictive(_distribution()),
        ticker="IREN", root="IREN", side="call", expiry=EXPIRY, strike=STRIKE,
        displayed_chain_fetched_at=datetime(2026, 9, 25, 21, tzinfo=UTC),
        displayed_chain_source="nasdaq",
    )
    assert result.predictive.status == "available"
    assert result.risk.status == "unavailable"
    assert result.risk.assumed_bid_cents is None
    assert result.greeks.source is None


@pytest.mark.parametrize(
    ("quote", "distribution"),
    [
        (_quote(session=date(2026, 9, 24), valuation=datetime(2026, 9, 24, 20, tzinfo=UTC)),
         _distribution(as_of=date(2026, 9, 25))),
        (_quote(session=EXPIRY, valuation=session_close(EXPIRY)),
         _distribution(expiry=EXPIRY)),
    ],
)
def test_quote_outside_forecast_window_withholds_hypothetical_risk(
    quote: EntryQuote, distribution: PredictiveDistribution
) -> None:
    result = quant_for_contract(
        _Market(quote), _Predictive(distribution),
        ticker="IREN", root="IREN", side="call", expiry=EXPIRY, strike=STRIKE,
    )
    assert result.predictive.status == "available"
    assert result.risk.status == "unavailable"
    assert result.risk.expected_pnl_cents is None


def test_intraday_entry_uses_only_remaining_forecast_time() -> None:
    as_of = date(2026, 9, 24)
    expiry = date(2026, 9, 25)
    entry = datetime(2026, 9, 25, 15, tzinfo=UTC)
    result = quant_for_contract(
        _Market(_quote(valuation=entry)),
        _Predictive(_distribution(as_of=as_of, expiry=expiry)),
        ticker="IREN", root="IREN", side="put", expiry=expiry, strike=STRIKE,
    )
    assert result.risk.status == "available"
    assert result.risk.quote_session == expiry
    fraction = remaining_session_variance_fraction(as_of, expiry, entry)
    assert fraction is not None
    scale = sqrt(float(fraction))
    terminal_low = 100 * (0.8**scale)
    terminal_high = 100 * (1.2**scale)
    pnl = (terminal_low - 95, 5, 5)
    assert result.risk.expected_pnl_cents == round(
        (pnl[0] * 0.2 + pnl[1] * 0.5 + pnl[2] * 0.3) * 10_000
    )
    assert result.risk.p05_pnl_cents == round(pnl[0] * 10_000)
    assert terminal_high > 100


def test_zero_pnl_is_not_a_loss_and_fifth_percentile_uses_lower_tail() -> None:
    from options_api.hypothetical_risk import compute_hypothetical_risk

    distribution = SimpleNamespace(
        status="available", spot=100.0,
        prices=(50.0, 95.0, 100.0, 125.0),
        weights=(0.05, 0.25, 0.25, 0.45),
    )
    result = compute_hypothetical_risk(
        distribution, side="put", strike=STRIKE, spot=STRIKE,
        bid=Decimal("5"), quote_source="nasdaq", quote_session=date(2026, 9, 25),
    )
    assert result.status == "available"
    assert result.loss_pct_tenths == 50  # Only the 5% scenario is below zero.
    assert result.p05_pnl_cents == -450_000


@pytest.mark.asyncio
async def test_watchlist_api_keeps_all_three_quant_views_separate() -> None:
    market = _Market(_quote())
    predictive = _Predictive(_distribution())
    item = WatchItem(
        id="a" * 32, ticker="IREN", root="IREN", side="call",
        expiration=EXPIRY, strike_exact="100.000", terms_note="Standard terms assumed",
        created_at=datetime(2026, 9, 25, 19, tzinfo=UTC),
        outcome=OutcomeView(status="pending"),
    )
    state = SimpleNamespace(
        clock=lambda: datetime(2026, 9, 25, 21, tzinfo=UTC),
        watchlist=SimpleNamespace(items=lambda *, as_of: [item]),
        market_odds=market, predictive_odds=predictive,
        jobs=SimpleNamespace(active=lambda _kind: None),
    )
    response = await get_watchlist(SimpleNamespace(app=SimpleNamespace(state=state)))
    serialized = response.model_dump(mode="json")["items"][0]

    assert market.scheduled == predictive.scheduled == ["IREN"]
    assert serialized["market_odds"]["status"] == "unavailable"
    assert serialized["last_available_market_odds"]["status"] == "available"
    assert serialized["predictive_odds"]["status"] == "available"
    assert serialized["predictive_odds"]["as_of_session"] == "2026-09-25"
    assert serialized["hypothetical_risk"]["status"] == "available"
    assert serialized["hypothetical_risk"]["quote_session"] == "2026-09-25"
