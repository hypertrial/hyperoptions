from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise

import pytest
import regimelib as rl

from options_api.market_odds import (
    _Quote,
    _price_converged,
    _vertical_bounds,
    _years_to_close,
    calculate_market_odds,
)
from options_api.models import OptionQuote


NOW = datetime(2026, 9, 11, 14, tzinfo=UTC)
EXPIRIES = (date(2026, 10, 16), date(2026, 11, 20), date(2027, 1, 8), date(2027, 5, 19))


def _normal_cdf(value: float) -> float:
    return (1 + math.erf(value / math.sqrt(2))) / 2


def _black_call(spot: float, strike: float, years: float, rate: float, sigma: float) -> float:
    d1 = (math.log(spot / strike) + (rate + sigma**2 / 2) * years) / (sigma * math.sqrt(years))
    d2 = d1 - sigma * math.sqrt(years)
    return spot * _normal_cdf(d1) - strike * math.exp(-rate * years) * _normal_cdf(d2)


def _row(expiry: date, strike: Decimal, call_bid: Decimal, call_ask: Decimal) -> OptionQuote:
    return OptionQuote(
        ticker="TEST", root="TEST", expiration=expiry.isoformat(), strike=strike,
        call_bid=call_bid, call_ask=call_ask, call_open_interest=100,
        put_bid=Decimal("1"), put_ask=Decimal("1.1"), put_open_interest=100,
    )


def _call_quote(strike: str, *, half_spread: float) -> _Quote:
    expiry = date(2026, 11, 20)
    years = _years_to_close(expiry, NOW)
    mid = _black_call(100, float(strike), years, 0.04, 0.25)
    return _Quote(expiry.isoformat(), Decimal(strike), years, 0.04,
                  mid - half_spread, mid + half_spread)


def test_equal_volatility_regime_prices_match_known_black_scholes_limit() -> None:
    model = rl.SwitchingBlackScholesProcess(
        rl.RegimeChain.twoState(2.0, 2.0), 100.0, 0.04, 0.0, [0.25, 0.25]
    )
    quote = _Quote("2027-09-17", Decimal("100"), 1.0, 0.04, 9.0, 10.0)
    vanilla = _price_converged(model, quote, digital=False)
    digital = _price_converged(model, quote, digital=True)
    assert vanilla == pytest.approx(_black_call(100, 100, 1, 0.04, 0.25), abs=0.01)
    d2 = (0.04 - 0.25**2 / 2) / 0.25
    assert digital is not None
    assert digital * math.exp(0.04) == pytest.approx(_normal_cdf(d2), abs=0.005)


def test_short_dated_low_volatility_tail_is_withheld_for_upstream_issue_2() -> None:
    model = rl.SwitchingBlackScholesProcess(
        rl.RegimeChain.twoState(2.0, 2.0), 100.0, 0.04, 0.0, [0.08, 0.4]
    )
    quote = _Quote("2026-09-12", Decimal("120"), 1 / 365, 0.04, 0.01, 0.02)
    assert _price_converged(model, quote, digital=True) is None
    assert _price_converged(model, quote, digital=False) is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"root": "OTHER"}, "invalid_contract"),
        ({"call_bid": None}, "invalid_quote"),
        ({"call_bid": Decimal("2"), "call_ask": Decimal("1")}, "invalid_quote"),
        ({"call_open_interest": 0, "call_volume": 0}, "invalid_quote"),
    ],
)
def test_malformed_or_nonstandard_quotes_never_publish_odds(change: dict, expected: str) -> None:
    expiry = date(2026, 11, 20)
    row = _row(expiry, Decimal("100"), Decimal("3"), Decimal("3.1")).model_copy(update=change)
    result = calculate_market_odds([row], Decimal("100"), lambda _: 0.04,
                                   {expiry.isoformat()}, NOW)
    assert result[(expiry.isoformat(), Decimal("100"))].call_itm_probability is None
    assert result[(expiry.isoformat(), Decimal("100"))].reason == expected


def test_same_day_missing_rate_invalid_spot_and_sparse_chain_withhold_odds() -> None:
    future = date(2026, 11, 20)
    row = _row(future, Decimal("100"), Decimal("3"), Decimal("3.1"))
    key = (future.isoformat(), Decimal("100"))
    assert calculate_market_odds([row], Decimal("0"), lambda _: 0.04,
                                 {future.isoformat()}, NOW)[key].reason == "invalid_spot"
    assert calculate_market_odds([row], Decimal("100"), lambda _: None,
                                 {future.isoformat()}, NOW)[key].reason == "rate_unavailable"
    assert calculate_market_odds([row], Decimal("100"), lambda _: -0.01,
                                 {future.isoformat()}, NOW)[key].reason == "rate_unavailable"
    assert calculate_market_odds([row], Decimal("100"), lambda _: 0.04,
                                 {future.isoformat()}, NOW)[key].reason == "calibration_failed"
    today = date(2026, 9, 11)
    same_day = _row(today, Decimal("100"), Decimal("3"), Decimal("3.1"))
    today_result = calculate_market_odds(
        [same_day], Decimal("100"), lambda _: 0.04, {today.isoformat()}, NOW
    )
    assert today_result[(today.isoformat(), Decimal("100"))].reason == "same_day_quote_timing"


def test_dense_black_scholes_surface_publishes_monotone_accurate_odds() -> None:
    spot, rate, sigma = 100.0, 0.04, 0.25
    rows: list[OptionQuote] = []
    for expiry in EXPIRIES:
        years = _years_to_close(expiry, NOW)
        for strike in range(93, 108):
            mid = _black_call(spot, strike, years, rate, sigma)
            rows.append(_row(
                expiry, Decimal(strike), Decimal(str(mid - 0.01)), Decimal(str(mid + 0.01))
            ))
    odds = calculate_market_odds(
        rows, Decimal("100"), lambda _: rate, {day.isoformat() for day in EXPIRIES}, NOW
    )
    assert len(odds) == len(rows)
    for expiry in EXPIRIES[1:]:
        years = _years_to_close(expiry, NOW)
        d2 = (rate - sigma**2 / 2) * years / (sigma * math.sqrt(years))
        atm = odds[(expiry.isoformat(), Decimal("100"))].call_itm_probability
        assert atm is not None, (expiry, odds[(expiry.isoformat(), Decimal("100"))])
        assert atm == pytest.approx(_normal_cdf(d2), abs=0.03)
        published = [
            odds[(expiry.isoformat(), Decimal(strike))].call_itm_probability
            for strike in range(93, 108)
        ]
        probabilities = [value for value in published if value is not None]
        assert len(probabilities) >= 5
        assert all(left >= right for left, right in pairwise(probabilities))


def test_sparse_strike_spacing_withholds_when_quote_bounds_are_too_wide() -> None:
    expiry = date(2026, 11, 20)
    years = _years_to_close(expiry, NOW)
    rows = [
        _row(
            expiry, Decimal(strike),
            Decimal(str(_black_call(100, strike, years, 0.04, 0.25) - 0.05)),
            Decimal(str(_black_call(100, strike, years, 0.04, 0.25) + 0.05)),
        )
        for strike in (90, 95, 100, 105, 110)
    ]
    result = calculate_market_odds(rows, Decimal("100"), lambda _: 0.04,
                                   {expiry.isoformat()}, NOW)
    assert all(estimate.call_itm_probability is None for estimate in result.values())


def test_corrupted_held_out_market_quote_rejects_the_shared_fit() -> None:
    rows: list[OptionQuote] = []
    for expiry in EXPIRIES:
        years = _years_to_close(expiry, NOW)
        for strike in range(93, 108):
            mid = _black_call(100, strike, years, 0.04, 0.25)
            if expiry == EXPIRIES[1] and strike == 98:
                mid += 2.0  # This strike is held out of the calibration sample.
            rows.append(_row(
                expiry, Decimal(strike), Decimal(str(mid - 0.01)), Decimal(str(mid + 0.01))
            ))
    odds = calculate_market_odds(
        rows, Decimal("100"), lambda _: 0.04,
        {expiry.isoformat() for expiry in EXPIRIES}, NOW,
    )
    assert odds[(EXPIRIES[2].isoformat(), Decimal("100"))].reason == "calibration_failed"


def test_wider_clean_verticals_rescue_tight_bound_when_adjacent_quotes_cannot() -> None:
    quotes = [
        _call_quote(strike, half_spread=0.01)
        for strike in ("99", "99.9", "100", "100.1", "101")
    ]
    target = quotes[2]
    growth = math.exp(target.rate * target.years)
    adjacent_lower = (target.bid - quotes[3].ask) / 0.1 * growth
    adjacent_upper = (quotes[1].ask - target.bid) / 0.1 * growth
    assert adjacent_upper - adjacent_lower > 0.10

    bounds = _vertical_bounds(quotes, target)
    assert bounds is not None
    assert 0 < bounds[1] - bounds[0] <= 0.10
    d2 = (0.04 - 0.25**2 / 2) * math.sqrt(target.years) / 0.25
    assert bounds[0] <= _normal_cdf(d2) <= bounds[1]


def test_inconsistent_far_vertical_rejects_otherwise_narrow_adjacent_bound() -> None:
    quotes = [
        _call_quote(strike, half_spread=0.005)
        for strike in ("98", "99.5", "100", "100.5", "102")
    ]
    target = quotes[2]
    assert _vertical_bounds(quotes[1:4], target) is not None
    # A lower strike cannot have a call ask below the target call bid.
    far = quotes[0]
    invalid_ask = target.bid - 0.2
    quotes[0] = _Quote(far.expiration, far.strike, far.years, far.rate,
                       invalid_ask - 0.01, invalid_ask)
    assert quotes[0].bid > 0
    assert _vertical_bounds(quotes, target) is None
