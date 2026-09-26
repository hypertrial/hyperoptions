"""Join dated option quotes with completed-close predictive distributions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from options_api.greeks import (
    ContractGreeks,
    compute_greeks,
    empty_greeks,
    years_until_expiry_close,
)
from options_api.hypothetical_risk import compute_hypothetical_risk
from options_api.market_calendar import remaining_session_variance_fraction
from options_api.market_watch import MarketWatchOdds
from options_api.models import (
    HypotheticalRiskView,
    MarketOddsView,
    MarketSource,
    PredictiveOddsView,
    Side,
)
from options_api.money import to_pct_tenths
from options_api.predictive_watch import PredictiveWatchOdds

@dataclass(frozen=True)
class LiveQuant:
    market: MarketOddsView
    predictive: PredictiveOddsView
    risk: HypotheticalRiskView
    greeks: ContractGreeks
    greeks_rate_pct_tenths: int | None = None
    greeks_rate_as_of_session: date | None = None
    last_available_market: MarketOddsView | None = None


def quant_for_contract(
    market_odds: MarketWatchOdds,
    predictive_odds: PredictiveWatchOdds,
    *,
    ticker: str,
    root: str,
    side: Side,
    expiry: date,
    strike: Decimal,
    contract_since: date | None = None,
    watched: bool = False,
    displayed_chain_fetched_at: datetime | None = None,
    displayed_chain_source: MarketSource | None = None,
) -> LiveQuant:
    expiry_text = expiry.isoformat()
    market = market_odds.lookup(ticker, side, expiry_text, strike, root)
    predictive, distribution = predictive_odds.lookup(
        ticker,
        side,
        expiry,
        strike,
        contract_since=contract_since,
    )
    last_good = (
        market_odds.lookup_last_good(ticker, side, expiry_text, strike, root)
        if watched and market.status != "available"
        else None
    )
    quote = market_odds.entry_quote(ticker, side, expiry_text, strike, root)
    if quote is not None and displayed_chain_fetched_at is not None and (
        quote.fetched_at != displayed_chain_fetched_at
        or quote.source != displayed_chain_source
    ):
        quote = None
    if quote is None:
        return LiveQuant(
            market,
            predictive,
            HypotheticalRiskView(reason="Coherent entry quotes are unavailable"),
            empty_greeks(),
            last_available_market=last_good,
        )

    years = years_until_expiry_close(expiry, quote.valuation_time)
    greeks = (
        compute_greeks(
            side,
            quote.spot,
            strike,
            (expiry - quote.session_date).days,
            quote.rate,
            quote.bid,
            quote.ask,
            years_to_expiry=years,
        )
        if years > 0 and quote.rate is not None and quote.rate_as_of_session is not None
        else empty_greeks()
    )
    entry_spot = quote.stock_ask if side == "call" else quote.spot
    remaining_fraction = remaining_session_variance_fraction(
        distribution.as_of, expiry, quote.valuation_time
    )
    if entry_spot is None:
        risk = HypotheticalRiskView(reason="A stock purchase quote is unavailable")
    elif distribution.status != "available":
        risk = HypotheticalRiskView(reason="Predictive distribution is unavailable")
    elif remaining_fraction is None:
        risk = HypotheticalRiskView(reason="Forecast and entry quote dates do not align")
    else:
        risk = compute_hypothetical_risk(
            distribution,
            side=side,
            strike=strike,
            spot=entry_spot,
            bid=quote.bid,
            quote_source=quote.source,
            quote_session=quote.session_date,
            remaining_variance_fraction=remaining_fraction,
        )
    return LiveQuant(
        market,
        predictive,
        risk,
        greeks,
        greeks_rate_pct_tenths=(
            to_pct_tenths(quote.rate * 100) if greeks.source is not None else None
        ),
        greeks_rate_as_of_session=(
            quote.rate_as_of_session if greeks.source is not None else None
        ),
        last_available_market=last_good,
    )
