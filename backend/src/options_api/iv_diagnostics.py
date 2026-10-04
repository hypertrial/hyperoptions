"""Chain-only quote sensitivity and exact, dated Black-Scholes inputs."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from options_api.greeks import (
    BOUND_EPSILON,
    PRICE_TOL,
    SIGMA_HI,
    SIGMA_LO,
    ContractGreeks,
    black_scholes_price,
    endpoint_iv,
    no_arb_bounds,
    years_until_expiry_close,
)
from options_api.market_calendar import session_close, session_on_or_before
from options_api.models import IvDetails, IvPricingPath, IvReason, Side
from options_api.pricing_context import EntryQuote, PricingIssue


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _midpoint_reason(
    side: Side, quote: EntryQuote, strike: Decimal, years: Decimal, midpoint: Decimal,
) -> IvReason:
    # Diagnose the legacy result without running a second midpoint inversion.
    assert quote.rate is not None
    lower, upper = no_arb_bounds(side == "call", quote.spot, strike, years, quote.rate)
    if side == "put":
        upper = strike * (-quote.rate * years).exp()
    if midpoint <= lower + BOUND_EPSILON:
        return IvReason(
            code="midpoint_eligibility",
            message="Midpoint does not exceed the model lower bound by the required $0.01 buffer",
        )
    if midpoint >= upper:
        return IvReason(code="model_bounds", message="Midpoint is outside European model bounds")
    args = (side == "call", float(quote.spot), float(strike), float(years), float(quote.rate))
    low = black_scholes_price(*args, SIGMA_LO)
    high = black_scholes_price(*args, SIGMA_HI)
    if float(midpoint) < low - PRICE_TOL or float(midpoint) > high + PRICE_TOL:
        return IvReason(
            code="solver_range",
            message="Midpoint is outside the 0.01%-500% volatility search range",
        )
    return IvReason(code="numerical_failure", message="Pricing calculation did not converge")


def iv_details_for_contract(
    *, side: Side, expiry: date, strike: Decimal, quote: EntryQuote | None,
    greeks: ContractGreeks, issue: PricingIssue | None, pricing_path: IvPricingPath,
    midpoint_issue: IvReason | None = None,
    display_rate: Decimal | None = None,
) -> IvDetails:
    reason = issue.view() if issue is not None else None
    pending = issue is not None and issue.code == "input_pending"
    details = IvDetails(
        status=(
            "pending" if pending
            else "available" if greeks.iv_pct_tenths is not None
            else "unavailable"
        ),
        reason=reason,
        strike_exact=format(strike, "f"),
        expiry_close=_utc(session_close(session_on_or_before(expiry))),
    )
    if quote is None:
        reason = reason or IvReason(
            code="option_quote", message="Coherent option entry quotes are unavailable"
        )
        return details.model_copy(update={
            "status": "pending" if pending else "unavailable",
            "reason": reason, "bid_reason": reason, "ask_reason": reason,
        })
    years = years_until_expiry_close(expiry, quote.valuation_time)
    mid = (quote.bid + quote.ask) / 2
    shown_rate = quote.rate if quote.rate is not None else display_rate
    details = details.model_copy(update={
        "spot_exact": format(quote.spot, "f"),
        "bid_price_exact": format(quote.bid, "f"),
        "mid_price_exact": format(mid, "f"),
        "ask_price_exact": format(quote.ask, "f"),
        "rate_exact": format(shown_rate, "f") if shown_rate is not None else None,
        "years_to_expiry_exact": format(years, "f") if years > 0 else None,
        "spot_basis": quote.spot_basis,
        "pricing_path": pricing_path,
        "chain_source": quote.source,
        "valuation_time": _utc(quote.valuation_time),
        "underlying_quote_time": _utc(quote.underlying_quote_time),
        "option_chain_fetched_at": _utc(quote.fetched_at),
        "quote_session_date": quote.session_date,
        "rate_as_of_session": quote.rate_as_of_session,
    })
    if reason is None and years <= 0:
        reason = IvReason(code="expiry_timing", message="Expiry trading session has completed")
    if reason is None and (quote.rate is None or quote.rate_as_of_session is None):
        reason = IvReason(code="treasury", message="Dated Treasury rate is unavailable")
    if reason is not None:
        return details.model_copy(update={
            "status": "pending" if pending else "unavailable",
            "reason": reason, "bid_reason": reason, "ask_reason": reason,
        })
    assert quote.rate is not None
    reason = midpoint_issue
    bid, bid_reason = endpoint_iv(side, quote.spot, strike, years, quote.rate, quote.bid)
    ask, ask_reason = endpoint_iv(side, quote.spot, strike, years, quote.rate, quote.ask)
    if greeks.iv_pct_tenths is None and reason is None:
        try:
            reason = _midpoint_reason(side, quote, strike, years, mid)
        except (ArithmeticError, ValueError):
            reason = IvReason(
                code="numerical_failure", message="Pricing calculation did not converge"
            )
    return details.model_copy(update={
        "reason": reason, "bid_pct_tenths": bid, "ask_pct_tenths": ask,
        "bid_reason": bid_reason, "ask_reason": ask_reason,
    })
