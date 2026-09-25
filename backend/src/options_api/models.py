from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

Ticker = str
TICKER_PATTERN = re.compile(r"^[A-Z]{1,5}$")
CurrentSource = Literal["stock_bid", "chain_last_trade"]
Moneyness = Literal["itm", "otm", "all"]
Side = Literal["call", "put"]
GreeksSource = Literal["bid", "mid"]


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


class CoveredCallContract(BaseModel):
    expiration: str
    dte: int
    strike_cents: int
    in_the_money: bool
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
    vs_7d_low_pct_tenths: int | None
    vs_30d_low_pct_tenths: int | None
    vs_90d_low_pct_tenths: int | None
    vs_365d_low_pct_tenths: int | None
    iv_pct_tenths: int | None = None
    delta_e4: int | None = None
    gamma_e4: int | None = None
    theta_e4: int | None = None
    vega_e4: int | None = None
    rho_e4: int | None = None
    greeks_source: GreeksSource | None = None


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
    chain_from_cache: bool
    info_from_cache: bool
    history_from_cache: bool
    risk_free_rate_pct_tenths: int
    lows: PeriodLows


class CoveredCallPage(_ChainPageBase):
    expirations: list[CoveredCallExpiration]


class CashSecuredPutContract(BaseModel):
    expiration: str
    dte: int
    strike_cents: int
    in_the_money: bool
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
    vs_7d_low_pct_tenths: int | None
    vs_30d_low_pct_tenths: int | None
    vs_90d_low_pct_tenths: int | None
    vs_365d_low_pct_tenths: int | None
    iv_pct_tenths: int | None = None
    delta_e4: int | None = None
    gamma_e4: int | None = None
    theta_e4: int | None = None
    vega_e4: int | None = None
    rho_e4: int | None = None
    greeks_source: GreeksSource | None = None


class CashSecuredPutExpiration(BaseModel):
    expiration: str
    dte: int
    contracts: list[CashSecuredPutContract]


class CashSecuredPutPage(_ChainPageBase):
    expirations: list[CashSecuredPutExpiration]


class HealthResponse(BaseModel):
    ok: bool = True
