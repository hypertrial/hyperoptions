"""European Black-Scholes Greeks with no dividends.

This is an approximation of American equity options. Implied volatility is
solved from the sell bid first, then mid, and is omitted when the chosen
price violates no-arbitrage bounds.
"""

from __future__ import annotations

import math
import os
from decimal import Decimal
from typing import Literal

from options_api.models import GreeksSource
from options_api.money import (
    DAYS_PER_YEAR,
    HUNDRED,
    ZERO,
    optional_e4,
    optional_pct_tenths,
    parse_decimal,
    usable_price,
)

SQRT_2 = math.sqrt(2.0)
SIGMA_LO = 1e-4
SIGMA_HI = 5.0
PRICE_TOL = 1e-6
MAX_BISECTIONS = 80
BOUND_EPSILON = Decimal("0.01")
DEFAULT_RISK_FREE_RATE = Decimal("0.04")


def risk_free_rate() -> Decimal:
    parsed = parse_decimal(os.environ.get("OPTIONS_RISK_FREE_RATE", "0.04"))
    if parsed is None or parsed < ZERO:
        return DEFAULT_RISK_FREE_RATE
    return parsed


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT_2))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes_price(
    is_call: bool, spot: float, strike: float, years: float, rate: float, sigma: float
) -> float:
    if years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / (
        sigma * root_t
    )
    d2 = d1 - sigma * root_t
    discount = math.exp(-rate * years)
    if is_call:
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(
    is_call: bool, spot: float, strike: float, years: float, rate: float, price: float
) -> float | None:
    if years <= 0 or spot <= 0 or strike <= 0 or price <= 0:
        return None
    lo = SIGMA_LO
    hi = SIGMA_HI
    low_price = black_scholes_price(is_call, spot, strike, years, rate, lo)
    high_price = black_scholes_price(is_call, spot, strike, years, rate, hi)
    if price < low_price - PRICE_TOL or price > high_price + PRICE_TOL:
        return None
    for _ in range(MAX_BISECTIONS):
        mid = 0.5 * (lo + hi)
        model = black_scholes_price(is_call, spot, strike, years, rate, mid)
        if abs(model - price) <= PRICE_TOL:
            return mid
        if model < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def no_arb_bounds(
    is_call: bool, spot: Decimal, strike: Decimal, years: Decimal, rate: Decimal
) -> tuple[Decimal, Decimal]:
    discount = (-rate * years).exp()
    if is_call:
        lower = max(ZERO, spot - strike * discount)
        return lower, spot
    lower = max(ZERO, strike * discount - spot)
    return lower, strike


def _inside_bounds(price: Decimal, lower: Decimal, upper: Decimal) -> bool:
    return price > lower + BOUND_EPSILON and price < upper


def select_iv_price(
    is_call: bool,
    spot: Decimal,
    strike: Decimal,
    years: Decimal,
    rate: Decimal,
    bid: Decimal | None,
    ask: Decimal | None,
) -> tuple[Decimal | None, GreeksSource | None]:
    lower, upper = no_arb_bounds(is_call, spot, strike, years, rate)
    usable_bid = usable_price(bid)
    if usable_bid is not None and _inside_bounds(usable_bid, lower, upper):
        return usable_bid, "bid"
    usable_ask = usable_price(ask)
    if usable_bid is not None and usable_ask is not None:
        mid = (usable_bid + usable_ask) / 2
        if _inside_bounds(mid, lower, upper):
            return mid, "mid"
    return None, None


class ContractGreeks:
    def __init__(
        self,
        source: GreeksSource | None,
        iv: Decimal | None,
        delta: Decimal | None,
        gamma: Decimal | None,
        theta: Decimal | None,
        vega: Decimal | None,
        rho: Decimal | None,
    ) -> None:
        self.source = source
        self.iv_pct_tenths = optional_pct_tenths(iv * HUNDRED if iv is not None else None)
        self.delta_e4 = optional_e4(delta)
        self.gamma_e4 = optional_e4(gamma)
        self.theta_e4 = optional_e4(theta)
        self.vega_e4 = optional_e4(vega)
        self.rho_e4 = optional_e4(rho)


def empty_greeks() -> ContractGreeks:
    return ContractGreeks(None, None, None, None, None, None, None)


def compute_greeks(
    side: Literal["call", "put"],
    spot: Decimal,
    strike: Decimal,
    dte: int,
    rate: Decimal,
    bid: Decimal | None,
    ask: Decimal | None,
) -> ContractGreeks:
    if dte <= 0 or spot <= ZERO or strike <= ZERO:
        return empty_greeks()
    years = Decimal(dte) / DAYS_PER_YEAR
    is_call = side == "call"
    price, source = select_iv_price(is_call, spot, strike, years, rate, bid, ask)
    if price is None or source is None:
        return empty_greeks()
    sigma = implied_vol(
        is_call, float(spot), float(strike), float(years), float(rate), float(price)
    )
    if sigma is None:
        return empty_greeks()
    root_t = math.sqrt(float(years))
    spot_f = float(spot)
    strike_f = float(strike)
    rate_f = float(rate)
    d1 = (math.log(spot_f / strike_f) + (rate_f + 0.5 * sigma * sigma) * float(years)) / (
        sigma * root_t
    )
    d2 = d1 - sigma * root_t
    pdf = _norm_pdf(d1)
    discount = math.exp(-rate_f * float(years))
    if is_call:
        delta = _norm_cdf(d1)
        theta_year = (
            -spot_f * pdf * sigma / (2.0 * root_t)
            - rate_f * strike_f * discount * _norm_cdf(d2)
        )
        rho_unit = strike_f * float(years) * discount * _norm_cdf(d2)
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_year = (
            -spot_f * pdf * sigma / (2.0 * root_t)
            + rate_f * strike_f * discount * _norm_cdf(-d2)
        )
        rho_unit = -strike_f * float(years) * discount * _norm_cdf(-d2)
    gamma = pdf / (spot_f * sigma * root_t)
    vega_point = spot_f * pdf * root_t / 100.0
    theta_day = theta_year / float(DAYS_PER_YEAR)
    rho_pct = rho_unit / 100.0
    return ContractGreeks(
        source,
        Decimal(str(sigma)),
        Decimal(str(delta)),
        Decimal(str(gamma)),
        Decimal(str(theta_day)),
        Decimal(str(vega_point)),
        Decimal(str(rho_pct)),
    )
