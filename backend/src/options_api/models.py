from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Ticker = str
TICKER_PATTERN = re.compile(r"^[A-Z]{1,5}$")
CurrentSource = Literal["stock_bid", "chain_last_trade", "yahoo_underlying"]
Moneyness = Literal["itm", "otm", "all"]
ChainMoneyness = Literal["itm", "otm"]
Side = Literal["call", "put"]
GreeksSource = Literal["bid", "mid"]
MarketSource = Literal["nasdaq", "yahoo"]
IvReasonCode = Literal[
    "contract_identity", "chain_session", "underlying", "expiry_timing", "treasury",
    "dividends", "option_quote", "midpoint_eligibility", "model_bounds", "solver_range",
    "input_acquisition", "numerical_failure",
]
IvSpotBasis = Literal[
    "underlying_midpoint", "yahoo_regular_market_price", "completed_session_close",
]
IvPricingPath = Literal["displayed_chain", "matching_snapshot"]
PhysicalModel = Literal[
    "lognormal_ewma", "empirical_scaled", "student_t_ewma", "gjr_garch_t",
    "ohlc_har", "skew_t_ewma", "egarch_skew_t", "markov_switching",
    "ngboost_pooled", "earnings_jump", "iv_physical", "intraday_shadow",
]


def normalize_ticker(raw: str) -> str | None:
    text = raw.strip().upper()
    if TICKER_PATTERN.fullmatch(text):
        return text
    return None


class OptionQuote(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: Ticker
    expiration: str
    strike: Decimal
    root: str | None = None
    identity_reason: str | None = None
    call_bid: Decimal | None
    call_ask: Decimal | None
    call_volume: int | None = None
    call_open_interest: int | None = None
    put_bid: Decimal | None = None
    put_ask: Decimal | None = None
    put_volume: int | None = None
    put_open_interest: int | None = None


class OptionChainResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: Ticker
    fetched_at: datetime
    from_cache: bool
    last_trade: str | None
    last_trade_timestamp: str | None = None
    spot: Decimal | None = None
    source: Literal["nasdaq", "yahoo"] = "nasdaq"
    truncated: bool = False
    options_available: bool = True
    rows: list[OptionQuote]


class StockInfoResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: Ticker
    fetched_at: datetime
    from_cache: bool
    bid: Decimal | None
    ask: Decimal | None
    quote_timestamp: str | None
    is_real_time: bool
    market_session: str | None


class HistoricalBar(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    date: date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal
    volume: int | None = None


class HistoricalResponse(BaseModel):
    ticker: Ticker
    fetched_at: datetime
    from_cache: bool
    bars: list[HistoricalBar]


class PeriodLows(BaseModel):
    d7_cents: int | None
    d30_cents: int | None
    d90_cents: int | None
    d365_cents: int | None


class TickerListing(BaseModel):
    symbol: str
    name: str
    sector: str | None = None
    industry: str | None = None


class TickerSearchResponse(BaseModel):
    as_of: datetime
    total: int
    results: list[TickerListing]


class MarketOddsView(BaseModel):
    method: Literal["regimelib", "constrained_call_curve", "ssvi"] | None = None
    status: Literal["pending", "available", "unavailable"] = "pending"
    itm_pct_tenths: int | None = None
    otm_pct_tenths: int | None = None
    reason: str | None = None
    source: MarketSource | None = None
    fetched_at: datetime | None = None
    session_date: date | None = None
    model_version: str | None = None
    bound_low_pct_tenths: int | None = None
    bound_high_pct_tenths: int | None = None
    quote_support_score: int | None = None
    model_evidence: dict[str, object] | None = None


class PredictiveValidationEvidence(BaseModel):
    """Comparable calibration from independent, prospective as-issued forecasts."""

    source: Literal["prospective_as_issued"]
    option_side: Side
    model_version: str
    horizon_band: str
    moneyness_band: str
    independent_units: int = Field(ge=1)
    predicted_itm_pct_tenths: int = Field(ge=0, le=1000)
    observed_itm_pct_tenths: int = Field(ge=0, le=1000)
    through_session: date


class PredictiveOddsView(BaseModel):
    """Physical expiry-close forecast, distinct from risk-neutral option odds."""

    status: Literal["pending", "available", "unavailable"] = "pending"
    method: PhysicalModel | None = None
    reason: str | None = None
    itm_pct_tenths: int | None = None
    otm_pct_tenths: int | None = None
    atm_pct_tenths: int | None = None
    as_of_session: date | None = None
    expiry_session: date | None = None
    model_version: str | None = None
    support: int | None = None
    independent_blocks: int | None = Field(default=None, ge=0)
    fit_ms: int | None = Field(default=None, ge=0)
    lookup_ms: int | None = Field(default=None, ge=0)
    simulation_error_95_pct_tenths: int | None = Field(default=None, ge=0)
    data_hash: str | None = None
    price_basis: Literal["completed_close", "validated_underlying_quote"] | None = None
    price_as_of: datetime | None = None
    validation_evidence: PredictiveValidationEvidence | None = None
    evidence_key: str | None = None
    model_evidence: dict[str, object] | None = None


class HypotheticalRiskView(BaseModel):
    """One-contract hold-to-expiry payoff from a dated, coherent entry quote."""

    status: Literal["available", "unavailable"] = "unavailable"
    forecast_method: PhysicalModel | None = None
    reason: str | None = None
    assumed_spot_cents: int | None = None
    assumed_bid_cents: int | None = None
    quote_source: MarketSource | None = None
    quote_session: date | None = None
    forecast_price_basis: Literal["completed_close", "intraday_quote"] | None = None
    expected_pnl_cents: int | None = None
    expected_return_pct_tenths: int | None = None
    loss_pct_tenths: int | None = None
    p05_pnl_cents: int | None = None


class IvReason(BaseModel):
    code: IvReasonCode
    message: str


class IvDetails(BaseModel):
    status: Literal["available", "unavailable"]
    reason: IvReason | None = None
    bid_pct_tenths: int | None = None
    ask_pct_tenths: int | None = None
    bid_reason: IvReason | None = None
    ask_reason: IvReason | None = None
    model: Literal["black_scholes_european_no_dividends_v1"] = (
        "black_scholes_european_no_dividends_v1"
    )
    spot_exact: str | None = None
    strike_exact: str | None = None
    bid_price_exact: str | None = None
    mid_price_exact: str | None = None
    ask_price_exact: str | None = None
    rate_exact: str | None = None
    years_to_expiry_exact: str | None = None
    spot_basis: IvSpotBasis | None = None
    pricing_path: IvPricingPath | None = None
    chain_source: MarketSource | None = None
    valuation_time: datetime | None = None
    underlying_quote_time: datetime | None = None
    option_chain_fetched_at: datetime | None = None
    expiry_close: datetime | None = None
    quote_session_date: date | None = None
    rate_as_of_session: date | None = None


class CoveredCallContract(BaseModel):
    expiration: str
    dte: int
    strike_cents: int
    in_the_money: bool
    at_the_money: bool = False
    strike_exact: str = ""
    watch_key: str | None = None
    watchability_reason: str | None = None
    call_bid_cents: int | None
    call_ask_cents: int | None
    call_spread_cents: int | None
    call_spread_pct_tenths: int | None
    call_volume: int | None
    call_open_interest: int | None
    stock_cost_cents: int | None
    premium_cents: int | None
    outlay_cents: int | None
    effective_cost_cents: int | None
    called_pnl_cents: int | None
    called_pnl_per_share_cents: int | None
    simple_apr_pct_tenths: int | None
    stock_apr_pct_tenths: int | None
    drop_to_strike_pct_tenths: int
    drop_to_breakeven_pct_tenths: int | None
    net_premium_cents: int | None
    net_apr_pct_tenths: int | None
    breakeven_cents: int | None
    breakeven_change_pct_tenths: int | None
    vs_7d_low_pct_tenths: int | None
    vs_30d_low_pct_tenths: int | None
    vs_90d_low_pct_tenths: int | None
    vs_365d_low_pct_tenths: int | None
    iv_pct_tenths: int | None = None
    iv_details: IvDetails | None = None
    delta_e4: int | None = None
    gamma_e4: int | None = None
    theta_e4: int | None = None
    vega_e4: int | None = None
    rho_e4: int | None = None
    greeks_source: GreeksSource | None = None
    greeks_rate_pct_tenths: int | None = None
    greeks_rate_as_of_session: date | None = None
    market_odds: MarketOddsView = Field(default_factory=MarketOddsView)
    predictive_odds: PredictiveOddsView = Field(default_factory=PredictiveOddsView)
    physical_models: list[PredictiveOddsView] = Field(default_factory=list)
    market_models: list[MarketOddsView] = Field(default_factory=list)
    hypothetical_risk: HypotheticalRiskView = Field(default_factory=HypotheticalRiskView)


class CoveredCallExpiration(BaseModel):
    expiration: str
    dte: int
    contracts: list[CoveredCallContract]


class _ChainPageBase(BaseModel):
    ticker: Ticker
    name: str | None = None
    options_available: bool
    moneyness: Moneyness
    fetched_at: datetime
    chain_fetched_at: datetime
    current_cents: int | None
    current_source: CurrentSource | None
    stock_bid_cents: int | None
    stock_ask_cents: int | None
    market_session: str | None
    is_real_time: bool
    quote_timestamp: str | None
    last_trade: str | None
    last_trade_timestamp: str | None
    truncated: bool
    chain_source: MarketSource = "nasdaq"
    chain_from_cache: bool
    info_from_cache: bool
    history_from_cache: bool
    risk_free_rate_pct_tenths: int | None
    lows: PeriodLows
    model_evidence: dict[str, object] = Field(default_factory=dict)


class CoveredCallPage(_ChainPageBase):
    expirations: list[CoveredCallExpiration]


class CashSecuredPutContract(BaseModel):
    expiration: str
    dte: int
    strike_cents: int
    in_the_money: bool
    at_the_money: bool = False
    strike_exact: str = ""
    watch_key: str | None = None
    watchability_reason: str | None = None
    put_bid_cents: int | None
    put_ask_cents: int | None
    put_spread_cents: int | None
    put_spread_pct_tenths: int | None
    put_volume: int | None
    put_open_interest: int | None
    premium_cents: int | None
    collateral_cents: int | None
    net_collateral_cents: int | None
    breakeven_cents: int | None
    apr_collateral_pct_tenths: int | None
    apr_net_pct_tenths: int | None
    cushion_to_strike_pct_tenths: int
    cushion_to_breakeven_pct_tenths: int | None
    net_premium_cents: int | None
    net_apr_pct_tenths: int | None
    breakeven_change_pct_tenths: int | None
    vs_7d_low_pct_tenths: int | None
    vs_30d_low_pct_tenths: int | None
    vs_90d_low_pct_tenths: int | None
    vs_365d_low_pct_tenths: int | None
    iv_pct_tenths: int | None = None
    iv_details: IvDetails | None = None
    delta_e4: int | None = None
    gamma_e4: int | None = None
    theta_e4: int | None = None
    vega_e4: int | None = None
    rho_e4: int | None = None
    greeks_source: GreeksSource | None = None
    greeks_rate_pct_tenths: int | None = None
    greeks_rate_as_of_session: date | None = None
    market_odds: MarketOddsView = Field(default_factory=MarketOddsView)
    predictive_odds: PredictiveOddsView = Field(default_factory=PredictiveOddsView)
    physical_models: list[PredictiveOddsView] = Field(default_factory=list)
    market_models: list[MarketOddsView] = Field(default_factory=list)
    hypothetical_risk: HypotheticalRiskView = Field(default_factory=HypotheticalRiskView)


class CashSecuredPutExpiration(BaseModel):
    expiration: str
    dte: int
    contracts: list[CashSecuredPutContract]


class CashSecuredPutPage(_ChainPageBase):
    expirations: list[CashSecuredPutExpiration]


class HealthResponse(BaseModel):
    ok: bool = True
