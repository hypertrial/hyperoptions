"""Join dated option quotes with completed-close predictive distributions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from options_api.contract_identity import make_watch_key, strike_exact
from options_api.greeks import (
    ContractGreeks,
    compute_greeks,
    empty_greeks,
    years_until_expiry_close,
)
from options_api.hypothetical_risk import compute_hypothetical_risk
from options_api.market_calendar import session_close
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
from stocksweeper.forecast.ledger import ForecastIssuance
from stocksweeper.forecast.predictive import PredictiveDistribution


@dataclass(frozen=True)
class LiveQuant:
    market: MarketOddsView
    predictive: PredictiveOddsView
    risk: HypotheticalRiskView
    greeks: ContractGreeks
    greeks_rate_pct_tenths: int | None = None
    greeks_rate_as_of_session: date | None = None
    last_available_market: MarketOddsView | None = None
    issuance: tuple[ForecastIssuance, PredictiveDistribution | None] | None = None


def _issuance(
    predictive_odds: PredictiveWatchOdds,
    view: PredictiveOddsView,
    distribution: PredictiveDistribution,
    *,
    ticker: str,
    root: str,
    side: Side,
    expiry: date,
    strike: Decimal,
    contract_since: date | None,
    terms_note: str,
) -> tuple[PredictiveOddsView, tuple[ForecastIssuance, PredictiveDistribution | None]]:
    # An issuance is stamped by the real wall clock, even when the app's
    # injectable market clock is frozen for a backtest or local UI fixture.
    issued_at = datetime.now(UTC)
    available = view.status == "available"
    retrieved_at = (
        predictive_odds.cache_retrieved_at(ticker, distribution.data_hash, distribution.as_of)
        if available and distribution.data_hash is not None
        else None
    )
    if available and (retrieved_at is None or retrieved_at > issued_at):
        view = view.model_copy(
            update={
                "status": "unavailable",
                "reason": "input_provenance_unverified",
                "itm_pct_tenths": None,
                "otm_pct_tenths": None,
                "atm_pct_tenths": None,
            }
        )
        available = False
    call = distribution.probability("call", strike) if available else None
    put = distribution.probability("put", strike) if available else None
    itm = call if side == "call" else put
    otm = put if side == "call" else call
    atm = max(0.0, 1.0 - call - put) if available else None
    issue = ForecastIssuance(
        contract_key=make_watch_key(ticker, root, side, expiry.isoformat(), strike),
        ticker=ticker,
        root=root,
        side=side,
        expiration=expiry,
        expiry_session=distribution.expiry_session,
        strike_exact=strike_exact(strike),
        terms_note=terms_note,
        contract_since=contract_since or distribution.as_of,
        input_session=distribution.as_of,
        input_retrieved_at=retrieved_at,
        issued_at=issued_at,
        model_version=distribution.model_version,
        method=distribution.method,
        data_hash=distribution.data_hash,
        distribution_hash=None,
        price_basis=view.price_basis,
        spot_exact=(str(distribution.spot) if distribution.spot is not None else None),
        status="available" if available else "unavailable",
        itm_probability=itm,
        otm_probability=otm,
        atm_probability=atm,
        unavailable_reason=None if available else (view.reason or "forecast_unavailable"),
    )
    return view, (issue, distribution if available else None)


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
    terms_note: str = "Assuming standard 100-share terms.",
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
    issuance = None
    if isinstance(predictive_odds, PredictiveWatchOdds):
        predictive, issuance = _issuance(
            predictive_odds,
            predictive,
            distribution,
            ticker=ticker,
            root=root,
            side=side,
            expiry=expiry,
            strike=strike,
            contract_since=contract_since,
            terms_note=terms_note,
        )
    last_good = (
        market_odds.lookup_last_good(ticker, side, expiry_text, strike, root)
        if watched and market.status != "available"
        else None
    )
    quote = market_odds.entry_quote(ticker, side, expiry_text, strike, root)
    if (
        quote is not None
        and displayed_chain_fetched_at is not None
        and (
            quote.fetched_at != displayed_chain_fetched_at or quote.source != displayed_chain_source
        )
    ):
        quote = None
    if quote is None:
        return LiveQuant(
            market,
            predictive,
            HypotheticalRiskView(reason="Coherent entry quotes are unavailable"),
            empty_greeks(),
            last_available_market=last_good,
            issuance=issuance,
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
    coherent_window = (
        distribution.as_of <= quote.session_date <= distribution.expiry_session
        and quote.valuation_time < session_close(distribution.expiry_session)
    )
    if entry_spot is None:
        risk = HypotheticalRiskView(reason="A stock purchase quote is unavailable")
    elif predictive.status != "available":
        risk = HypotheticalRiskView(reason="Verified predictive distribution is unavailable")
    elif not coherent_window:
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
            reanchor=False,
        )
    return LiveQuant(
        market,
        predictive,
        risk,
        greeks,
        greeks_rate_pct_tenths=(
            to_pct_tenths(quote.rate * 100) if greeks.source is not None else None
        ),
        greeks_rate_as_of_session=(quote.rate_as_of_session if greeks.source is not None else None),
        last_available_market=last_good,
        issuance=issuance,
    )
