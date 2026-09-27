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
from options_api.intraday_shadow import _VERSION as INTRADAY_VERSION
from options_api.market_calendar import session_close
from options_api.market_watch import MarketWatchOdds
from options_api.models import (
    HypotheticalRiskView,
    MarketOddsView,
    MarketSource,
    PhysicalModel,
    PredictiveOddsView,
    Side,
)
from options_api.money import to_pct_tenths
from options_api.outcomes import TERMS_NOTE
from options_api.predictive_watch import PredictiveWatchOdds
from options_api.physical_shadow_capture import PhysicalShadowCapture
from stocksweeper.forecast.calibration import horizon_band
from stocksweeper.forecast.ledger import ForecastIssuance
from stocksweeper.forecast.predictive import PredictiveDistribution
from stocksweeper.forecast.predictive import BASELINE_VERSION
from stocksweeper.forecast.physical_contest import (
    EMPIRICAL_SHADOW_VERSION, GJR_VERSION, STUDENT_VERSION,
)

_MODEL_VERSIONS = {
    "lognormal_ewma": BASELINE_VERSION,
    "empirical_scaled": EMPIRICAL_SHADOW_VERSION,
    "student_t_ewma": STUDENT_VERSION,
    "gjr_garch_t": GJR_VERSION,
    "intraday_shadow": INTRADAY_VERSION,
}


@dataclass(frozen=True)
class LiveQuant:
    market: MarketOddsView
    predictive: PredictiveOddsView
    risk: HypotheticalRiskView
    greeks: ContractGreeks
    physical_models: tuple[PredictiveOddsView, ...] = ()
    market_models: tuple[MarketOddsView, ...] = ()
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
    terms_note: str = TERMS_NOTE,
    physical_shadow: PhysicalShadowCapture | None = None,
    forecast_model: PhysicalModel = "lognormal_ewma",
) -> LiveQuant:
    expiry_text = expiry.isoformat()
    market = market_odds.lookup(ticker, side, expiry_text, strike, root)
    predictive, distribution = predictive_odds.lookup(
        ticker,
        side,
        expiry,
        strike,
        contract_since=contract_since,
        standard_terms=root == ticker and terms_note == TERMS_NOTE,
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
    physical_models: tuple[PredictiveOddsView, ...] = ()
    selected_distribution = distribution
    if physical_shadow is not None:
        model_views = []
        quote_for_model = market_odds.underlying_quote(ticker)
        for method in (
            "lognormal_ewma", "empirical_scaled", "student_t_ewma", "gjr_garch_t",
            "intraday_shadow",
        ):
            if predictive.status == "unavailable":
                view = PredictiveOddsView(
                    method=method, status="unavailable", reason=predictive.reason,
                    as_of_session=distribution.as_of,
                    expiry_session=distribution.expiry_session,
                    model_version=_MODEL_VERSIONS[method],
                )
            else:
                candidate = physical_shadow.candidate(
                    distribution, method, expiry, contract_since or distribution.as_of,
                    quote_for_model if method == "intraday_shadow" else None,
                )
                choice = candidate.distribution
                if choice is None:
                    view = PredictiveOddsView(
                        method=method,
                        status=(
                            "pending" if candidate.reason == "candidate_not_prepared"
                            else "unavailable"
                        ),
                        reason=candidate.reason,
                        as_of_session=distribution.as_of,
                        expiry_session=distribution.expiry_session,
                        data_hash=distribution.data_hash,
                        model_version=_MODEL_VERSIONS[method],
                    )
                elif (
                    choice.as_of != distribution.as_of
                    or choice.expiry_session != distribution.expiry_session
                    or (method != "intraday_shadow" and choice.data_hash != distribution.data_hash)
                ):
                    view = PredictiveOddsView(
                        method=method, status="unavailable", reason="input_vintage_changed",
                        model_version=_MODEL_VERSIONS[method],
                    )
                else:
                    view = predictive_odds.view_for_distribution(
                        choice, side, strike,
                        price_basis=(
                            "validated_underlying_quote"
                            if method == "intraday_shadow" else "completed_close"
                        ),
                        price_as_of=(
                            quote_for_model.quote_time
                            if method == "intraday_shadow" and quote_for_model is not None
                            else None
                        ),
                    )
                    if method == forecast_model and view.status == "available":
                        selected_distribution = choice
            if view.evidence_key is None and (
                band := horizon_band(distribution.horizon_sessions)
            ):
                view = view.model_copy(update={
                    "evidence_key": f"{method}:{band}"
                })
            model_views.append(view)
        physical_models = tuple(model_views)
        predictive = next(view for view in physical_models if view.method == forecast_model)
    market = market.model_copy(update={"method": "regimelib"})
    market_report = market_odds.curve_shadow_report(ticker) if physical_shadow is not None else None
    market = market.model_copy(update={"model_evidence": {
        "held_out_inside": (
            market_report.get("benchmark_held_out_inside") if market_report else None
        ),
        "held_out_count": (
            market_report.get("benchmark_held_out_predicted") if market_report else None
        ),
        "paired_held_out_count": (
            market_report.get("paired_held_out_count") if market_report else None
        ),
        "fit_ms": market_report.get("benchmark_ms") if market_report else None,
        "one_tick_stable": None,
        "bid_ask_fit": None,
    }})
    market_models = (
        (market, market_odds.lookup_curve(ticker, side, expiry_text, strike, root))
        if physical_shadow is not None else (market,)
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
            HypotheticalRiskView(
                reason="Coherent entry quotes are unavailable", forecast_method=forecast_model
            ),
            empty_greeks(),
            physical_models=physical_models,
            market_models=market_models,
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
        selected_distribution.as_of <= quote.session_date <= selected_distribution.expiry_session
        and quote.valuation_time < session_close(selected_distribution.expiry_session)
    )
    if entry_spot is None:
        risk = HypotheticalRiskView(reason="A stock purchase quote is unavailable")
    elif predictive.status != "available":
        risk = HypotheticalRiskView(reason="Verified predictive distribution is unavailable")
    elif not coherent_window:
        risk = HypotheticalRiskView(reason="Forecast and entry quote dates do not align")
    else:
        risk = compute_hypothetical_risk(
            selected_distribution,
            side=side,
            strike=strike,
            spot=entry_spot,
            bid=quote.bid,
            quote_source=quote.source,
            quote_session=quote.session_date,
            reanchor=False,
        )
        if forecast_model == "intraday_shadow" and risk.status == "available":
            risk = risk.model_copy(update={"forecast_price_basis": "intraday_quote"})
    risk = risk.model_copy(update={"forecast_method": forecast_model})
    return LiveQuant(
        market,
        predictive,
        risk,
        greeks,
        physical_models=physical_models,
        market_models=market_models,
        greeks_rate_pct_tenths=(
            to_pct_tenths(quote.rate * 100) if greeks.source is not None else None
        ),
        greeks_rate_as_of_session=(quote.rate_as_of_session if greeks.source is not None else None),
        last_available_market=last_good,
        issuance=issuance,
    )
