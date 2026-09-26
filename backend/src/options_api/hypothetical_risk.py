"""One-contract expiry payoffs under a physical stock-price distribution."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from math import exp, isfinite, log, sqrt
from typing import Protocol

from options_api.models import HypotheticalRiskView, MarketSource, Side
from options_api.money import SHARES_PER_CONTRACT, to_cents, to_pct_tenths


class PriceDistribution(Protocol):
    status: str
    spot: float | Decimal | None
    prices: tuple[float, ...] | tuple[Decimal, ...]
    weights: tuple[float, ...] | tuple[Decimal, ...]


def compute_hypothetical_risk(
    distribution: PriceDistribution,
    *,
    side: Side,
    strike: Decimal,
    spot: Decimal,
    bid: Decimal,
    quote_source: MarketSource,
    quote_session: date,
    remaining_variance_fraction: Decimal = Decimal(1),
) -> HypotheticalRiskView:
    """Reanchor forecast returns to a quoted entry spot, then value expiry P&L.

    Log returns are scaled to the remaining expiry time before reanchoring.
    The call assumes one lot of stock bought at ``spot`` and one call sold at
    ``bid``. The put assumes one cash-secured put sold at ``bid``. Neither
    formula models assignment, dividends, fees, or an actual portfolio.
    """
    if distribution.status != "available":
        return HypotheticalRiskView(reason="Predictive distribution is unavailable")
    if (
        distribution.spot is None
        or not spot.is_finite()
        or not strike.is_finite()
        or not bid.is_finite()
        or spot <= 0
        or strike <= 0
        or bid < 0
        or not remaining_variance_fraction.is_finite()
        or not 0 < remaining_variance_fraction <= 1
    ):
        return HypotheticalRiskView(reason="Entry quote or model spot is invalid")
    anchor = Decimal(str(distribution.spot))
    if not anchor.is_finite() or anchor <= 0:
        return HypotheticalRiskView(reason="Predictive distribution has no valid spot")
    prices = distribution.prices
    weights = distribution.weights
    if not prices or len(prices) != len(weights):
        return HypotheticalRiskView(reason="Predictive distribution has no support")

    capital = spot - bid if side == "call" else strike - bid
    if capital <= 0:
        return HypotheticalRiskView(reason="Hypothetical capital is not positive")
    scenarios: list[tuple[Decimal, Decimal]] = []
    total_weight = Decimal(0)
    for raw_price, raw_weight in zip(prices, weights, strict=True):
        price = Decimal(str(raw_price))
        weight = Decimal(str(raw_weight))
        if not price.is_finite() or price <= 0 or not weight.is_finite() or weight < 0:
            return HypotheticalRiskView(reason="Predictive distribution is invalid")
        if remaining_variance_fraction == 1:
            terminal = price * spot / anchor
        else:
            scaled_ratio = exp(
                log(float(price / anchor)) * sqrt(float(remaining_variance_fraction))
            )
            if not isfinite(scaled_ratio):
                return HypotheticalRiskView(reason="Predictive distribution is invalid")
            terminal = spot * Decimal(str(scaled_ratio))
        per_share = (
            min(terminal, strike) - spot + bid
            if side == "call"
            else bid - max(strike - terminal, Decimal(0))
        )
        scenarios.append((per_share, weight))
        total_weight += weight
    if total_weight <= 0:
        return HypotheticalRiskView(reason="Predictive distribution is invalid")

    expected_per_share = sum(
        (pnl * weight for pnl, weight in scenarios), Decimal(0)
    ) / total_weight
    loss_weight = sum((weight for pnl, weight in scenarios if pnl < 0), Decimal(0))
    threshold = total_weight * Decimal("0.05")
    cumulative = Decimal(0)
    fifth = scenarios[0][0]
    for pnl, weight in sorted(scenarios):
        cumulative += weight
        fifth = pnl
        if cumulative >= threshold:
            break

    shares = Decimal(SHARES_PER_CONTRACT)
    return HypotheticalRiskView(
        status="available",
        assumed_spot_cents=to_cents(spot),
        assumed_bid_cents=to_cents(bid),
        quote_source=quote_source,
        quote_session=quote_session,
        expected_pnl_cents=to_cents(expected_per_share * shares),
        expected_return_pct_tenths=to_pct_tenths(expected_per_share / capital * 100),
        loss_pct_tenths=to_pct_tenths(loss_weight / total_weight * 100),
        p05_pnl_cents=to_cents(fifth * shares),
    )
