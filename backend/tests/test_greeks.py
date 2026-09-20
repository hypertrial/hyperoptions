from __future__ import annotations

from decimal import Decimal

from options_api.greeks import (
    black_scholes_price,
    compute_greeks,
    implied_vol,
    no_arb_bounds,
    select_iv_price,
)
from options_api.money import to_e4, to_pct_tenths


def test_atm_call_golden_values() -> None:
    price = black_scholes_price(True, 100.0, 100.0, 1.0, 0.05, 0.2)
    assert abs(price - 10.4506) < 0.001
    greeks = compute_greeks(
        "call",
        Decimal("100"),
        Decimal("100"),
        365,
        Decimal("0.05"),
        Decimal("10.4506"),
        Decimal("10.4506"),
    )
    assert greeks.source == "bid"
    assert greeks.iv_pct_tenths == to_pct_tenths(Decimal("20"))
    assert greeks.delta_e4 == to_e4(Decimal("0.6368"))
    assert greeks.gamma_e4 == to_e4(Decimal("0.01876"))
    assert greeks.vega_e4 == to_e4(Decimal("0.3752"))
    assert greeks.theta_e4 == to_e4(Decimal("-0.01757"))
    assert greeks.rho_e4 == to_e4(Decimal("0.5323"))


def test_put_call_parity_and_iv_round_trip() -> None:
    call = black_scholes_price(True, 100.0, 100.0, 1.0, 0.05, 0.2)
    put = black_scholes_price(False, 100.0, 100.0, 1.0, 0.05, 0.2)
    assert abs(call - put - (100.0 - 100.0 * 2.718281828459045 ** -0.05)) < 0.002
    recovered = implied_vol(True, 100.0, 100.0, 1.0, 0.05, call)
    assert recovered is not None
    assert abs(recovered - 0.2) < 1e-4


def test_bound_violations_and_zero_dte_are_null() -> None:
    empty = compute_greeks(
        "call", Decimal("100"), Decimal("90"), 0, Decimal("0.04"), Decimal("10"), Decimal("11")
    )
    assert empty.source is None
    assert empty.iv_pct_tenths is None
    assert empty.delta_e4 is None
    zero_price = compute_greeks(
        "call", Decimal("100"), Decimal("90"), 30, Decimal("0.04"), Decimal("0"), None
    )
    assert zero_price.source is None
    intrinsic = compute_greeks(
        "call",
        Decimal("100"),
        Decimal("90"),
        30,
        Decimal("0.04"),
        Decimal("10.00"),
        None,
    )
    assert intrinsic.source is None
    above_spot = select_iv_price(
        True,
        Decimal("100"),
        Decimal("90"),
        Decimal("30") / Decimal("365"),
        Decimal("0.04"),
        Decimal("100"),
        Decimal("101"),
    )
    assert above_spot == (None, None)
    above_strike = select_iv_price(
        False,
        Decimal("100"),
        Decimal("90"),
        Decimal("30") / Decimal("365"),
        Decimal("0.04"),
        Decimal("90"),
        Decimal("91"),
    )
    assert above_strike == (None, None)


def test_bid_inside_bounds_is_preferred_over_mid() -> None:
    price, source = select_iv_price(
        True,
        Decimal("100"),
        Decimal("100"),
        Decimal("1"),
        Decimal("0.05"),
        Decimal("10.45"),
        Decimal("10.55"),
    )
    assert source == "bid"
    assert price == Decimal("10.45")


def test_bid_at_or_below_lower_bound_falls_back_to_mid() -> None:
    years = Decimal("30") / Decimal("365")
    rate = Decimal("0.04")
    lower, upper = no_arb_bounds(True, Decimal("100"), Decimal("90"), years, rate)
    assert lower + Decimal("0.01") > Decimal("10.00")
    assert Decimal("10.50") < upper
    price, source = select_iv_price(
        True,
        Decimal("100"),
        Decimal("90"),
        years,
        rate,
        Decimal("10.00"),
        Decimal("11.00"),
    )
    assert source == "mid"
    assert price == Decimal("10.50")
    at_epsilon = select_iv_price(
        True,
        Decimal("100"),
        Decimal("90"),
        years,
        rate,
        lower + Decimal("0.01"),
        Decimal("11.00"),
    )
    assert at_epsilon[1] == "mid"
