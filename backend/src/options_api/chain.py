from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from options_api.greeks import compute_greeks, risk_free_rate
from options_api.models import (
    CashSecuredPutContract,
    CashSecuredPutExpiration,
    CashSecuredPutPage,
    CoveredCallContract,
    CoveredCallExpiration,
    CoveredCallPage,
    CurrentSource,
    HistoricalBar,
    HistoricalResponse,
    Moneyness,
    OptionChainResponse,
    OptionQuote,
    PeriodLows,
    Side,
    StockInfoResponse,
    Ticker,
)
from options_api.money import (
    DAYS_PER_YEAR,
    HUNDRED,
    SHARES_PER_CONTRACT,
    ZERO,
    optional_cents,
    optional_pct_tenths,
    to_cents,
    to_pct_tenths,
    usable_price,
)
from options_api.nasdaq import NasdaqError
from options_api.service import OptionChainService

_NY = ZoneInfo("America/New_York")
HISTORY_LOOKBACK_DAYS = 400
MIN_OPEN_INTEREST = 5


def today_new_york(now: datetime | None = None) -> date:
    current = now or datetime.now(_NY)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_NY)
    return current.astimezone(_NY).date()


def current_reference(
    bid: Decimal | None, last_trade: Decimal | None
) -> tuple[Decimal | None, CurrentSource | None]:
    bid_price = usable_price(bid)
    if bid_price is not None:
        return bid_price, "stock_bid"
    last_price = usable_price(last_trade)
    if last_price is not None:
        return last_price, "chain_last_trade"
    return None, None


def days_to_expiration(expiration: str, today: date) -> int | None:
    try:
        expiry = date.fromisoformat(expiration)
    except ValueError:
        return None
    return (expiry - today).days


def period_low(bars: list[HistoricalBar], today: date, days: int) -> Decimal | None:
    start = today - timedelta(days=days)
    lows = [
        bar.low
        for bar in bars
        if start <= bar.date < today
        and bar.low is not None
        and bar.low.is_finite()
        and bar.low > ZERO
    ]
    return min(lows) if lows else None


def _spread(
    bid: Decimal | None, ask: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    if bid is None or ask is None or not bid.is_finite() or not ask.is_finite():
        return None, None
    spread = ask - bid
    midpoint = (bid + ask) / 2
    spread_pct = spread / midpoint * HUNDRED if midpoint > ZERO else None
    return spread, spread_pct


def _ratio_pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= ZERO or not denominator.is_finite():
        return None
    return numerator / denominator * HUNDRED


def _empty_info(ticker: Ticker, fetched_at: datetime) -> StockInfoResponse:
    return StockInfoResponse(
        ticker=ticker,
        fetched_at=fetched_at,
        from_cache=False,
        bid=None,
        ask=None,
        quote_timestamp=None,
        is_real_time=False,
        market_session=None,
    )


def _empty_history(ticker: Ticker, fetched_at: datetime) -> HistoricalResponse:
    return HistoricalResponse(
        ticker=ticker, fetched_at=fetched_at, from_cache=False, bars=[]
    )


def _qualifying_open_interest(value: int | None) -> bool:
    return value is not None and value >= MIN_OPEN_INTEREST


def _passes_moneyness(in_the_money: bool, moneyness: Moneyness) -> bool:
    if moneyness == "all":
        return True
    if moneyness == "itm":
        return in_the_money
    return not in_the_money


def _vs_lows(
    strike: Decimal, lows: dict[str, Decimal | None]
) -> dict[str, int | None]:
    return {
        "vs_7d_low_pct_tenths": optional_pct_tenths(
            _ratio_pct(strike - lows["d7"], lows["d7"]) if lows["d7"] else None
        ),
        "vs_30d_low_pct_tenths": optional_pct_tenths(
            _ratio_pct(strike - lows["d30"], lows["d30"]) if lows["d30"] else None
        ),
        "vs_90d_low_pct_tenths": optional_pct_tenths(
            _ratio_pct(strike - lows["d90"], lows["d90"]) if lows["d90"] else None
        ),
        "vs_365d_low_pct_tenths": optional_pct_tenths(
            _ratio_pct(strike - lows["d365"], lows["d365"]) if lows["d365"] else None
        ),
    }


def _call_contract(
    row: OptionQuote,
    dte: int,
    current: Decimal,
    lows: dict[str, Decimal | None],
    rate: Decimal,
    in_the_money: bool,
) -> CoveredCallContract:
    bid = usable_price(row.call_bid)
    shares = Decimal(SHARES_PER_CONTRACT)
    stock_cost = shares * current
    premium = shares * bid if bid is not None else None
    outlay = stock_cost - premium if premium is not None else None
    effective_cost = current - bid if bid is not None else None
    called_pnl_per_share = row.strike + bid - current if bid is not None else None
    called_pnl = (
        premium - shares * (current - row.strike) if premium is not None else None
    )
    simple_apr_pct = None
    if outlay is not None and called_pnl is not None and outlay > ZERO and dte > 0:
        simple_apr_pct = called_pnl / outlay * DAYS_PER_YEAR / Decimal(dte) * HUNDRED
    stock_apr_pct = None
    if stock_cost > ZERO and called_pnl is not None and dte > 0:
        stock_apr_pct = called_pnl / stock_cost * DAYS_PER_YEAR / Decimal(dte) * HUNDRED
    spread, spread_pct = _spread(row.call_bid, row.call_ask)
    drop = _ratio_pct(current - row.strike, current)
    assert drop is not None
    drop_to_breakeven = (
        _ratio_pct(current - effective_cost, current)
        if effective_cost is not None
        else None
    )
    greeks = compute_greeks(
        "call", current, row.strike, dte, rate, row.call_bid, row.call_ask
    )
    return CoveredCallContract(
        expiration=row.expiration,
        dte=dte,
        strike_cents=to_cents(row.strike),
        in_the_money=in_the_money,
        call_bid_cents=optional_cents(row.call_bid),
        call_ask_cents=optional_cents(row.call_ask),
        call_spread_cents=optional_cents(spread),
        call_spread_pct_tenths=optional_pct_tenths(spread_pct),
        call_volume=row.call_volume,
        call_open_interest=row.call_open_interest,
        stock_cost_cents=optional_cents(stock_cost),
        premium_cents=optional_cents(premium),
        outlay_cents=optional_cents(outlay),
        effective_cost_cents=optional_cents(effective_cost),
        called_pnl_cents=optional_cents(called_pnl),
        called_pnl_per_share_cents=optional_cents(called_pnl_per_share),
        simple_apr_pct_tenths=optional_pct_tenths(simple_apr_pct),
        stock_apr_pct_tenths=optional_pct_tenths(stock_apr_pct),
        drop_to_strike_pct_tenths=to_pct_tenths(drop),
        drop_to_breakeven_pct_tenths=optional_pct_tenths(drop_to_breakeven),
        **_vs_lows(row.strike, lows),
        iv_pct_tenths=greeks.iv_pct_tenths,
        delta_e4=greeks.delta_e4,
        gamma_e4=greeks.gamma_e4,
        theta_e4=greeks.theta_e4,
        vega_e4=greeks.vega_e4,
        rho_e4=greeks.rho_e4,
        greeks_source=greeks.source,
    )


def _put_contract(
    row: OptionQuote,
    dte: int,
    current: Decimal,
    lows: dict[str, Decimal | None],
    rate: Decimal,
    in_the_money: bool,
) -> CashSecuredPutContract:
    bid = usable_price(row.put_bid)
    shares = Decimal(SHARES_PER_CONTRACT)
    premium = shares * bid if bid is not None else None
    collateral = shares * row.strike
    net_collateral = collateral - premium if premium is not None else None
    breakeven = row.strike - bid if bid is not None else None
    apr_collateral = None
    if premium is not None and collateral > ZERO and dte > 0:
        apr_collateral = premium / collateral * DAYS_PER_YEAR / Decimal(dte) * HUNDRED
    apr_net = None
    if premium is not None and net_collateral is not None and net_collateral > ZERO and dte > 0:
        apr_net = premium / net_collateral * DAYS_PER_YEAR / Decimal(dte) * HUNDRED
    spread, spread_pct = _spread(row.put_bid, row.put_ask)
    cushion = _ratio_pct(current - row.strike, current)
    assert cushion is not None
    cushion_be = (
        _ratio_pct(current - breakeven, current) if breakeven is not None else None
    )
    greeks = compute_greeks(
        "put", current, row.strike, dte, rate, row.put_bid, row.put_ask
    )
    return CashSecuredPutContract(
        expiration=row.expiration,
        dte=dte,
        strike_cents=to_cents(row.strike),
        in_the_money=in_the_money,
        put_bid_cents=optional_cents(row.put_bid),
        put_ask_cents=optional_cents(row.put_ask),
        put_spread_cents=optional_cents(spread),
        put_spread_pct_tenths=optional_pct_tenths(spread_pct),
        put_volume=row.put_volume,
        put_open_interest=row.put_open_interest,
        premium_cents=optional_cents(premium),
        collateral_cents=optional_cents(collateral),
        net_collateral_cents=optional_cents(net_collateral),
        breakeven_cents=optional_cents(breakeven),
        apr_collateral_pct_tenths=optional_pct_tenths(apr_collateral),
        apr_net_pct_tenths=optional_pct_tenths(apr_net),
        cushion_to_strike_pct_tenths=to_pct_tenths(cushion),
        cushion_to_breakeven_pct_tenths=optional_pct_tenths(cushion_be),
        **_vs_lows(row.strike, lows),
        iv_pct_tenths=greeks.iv_pct_tenths,
        delta_e4=greeks.delta_e4,
        gamma_e4=greeks.gamma_e4,
        theta_e4=greeks.theta_e4,
        vega_e4=greeks.vega_e4,
        rho_e4=greeks.rho_e4,
        greeks_source=greeks.source,
    )


def _page_fields(
    chain: OptionChainResponse,
    info: StockInfoResponse,
    history: HistoricalResponse,
    current: Decimal | None,
    source: CurrentSource | None,
    raw_lows: dict[str, Decimal | None],
    fetched_at: datetime,
    name: str | None,
    moneyness: Moneyness,
    rate: Decimal,
) -> dict[str, object]:
    return {
        "ticker": chain.ticker,
        "name": name,
        "options_available": chain.options_available,
        "moneyness": moneyness,
        "fetched_at": fetched_at,
        "current_cents": optional_cents(current),
        "current_source": source,
        "stock_bid_cents": optional_cents(info.bid),
        "stock_ask_cents": optional_cents(info.ask),
        "market_session": info.market_session,
        "is_real_time": info.is_real_time,
        "quote_timestamp": info.quote_timestamp,
        "last_trade": chain.last_trade,
        "last_trade_timestamp": chain.last_trade_timestamp,
        "truncated": chain.truncated,
        "chain_from_cache": chain.from_cache,
        "info_from_cache": info.from_cache,
        "history_from_cache": history.from_cache,
        "risk_free_rate_pct_tenths": to_pct_tenths(rate * HUNDRED),
        "lows": PeriodLows(
            d7_cents=optional_cents(raw_lows["d7"]),
            d30_cents=optional_cents(raw_lows["d30"]),
            d90_cents=optional_cents(raw_lows["d90"]),
            d365_cents=optional_cents(raw_lows["d365"]),
        ),
    }


def assemble_covered_calls(
    chain: OptionChainResponse,
    info: StockInfoResponse | None,
    history: HistoricalResponse | None,
    today: date,
    fetched_at: datetime,
    moneyness: Moneyness = "itm",
    name: str | None = None,
    rate: Decimal | None = None,
) -> CoveredCallPage:
    info = info or _empty_info(chain.ticker, fetched_at)
    history = history or _empty_history(chain.ticker, fetched_at)
    current, source = current_reference(info.bid, chain.spot)
    rate = rate if rate is not None else risk_free_rate()
    raw_lows = {
        "d7": period_low(history.bars, today, 7),
        "d30": period_low(history.bars, today, 30),
        "d90": period_low(history.bars, today, 90),
        "d365": period_low(history.bars, today, 365),
    }
    grouped: dict[str, list[CoveredCallContract]] = {}
    seen: set[tuple[str, Decimal]] = set()
    if current is not None and chain.options_available:
        for row in chain.rows:
            key = (row.expiration, row.strike)
            if key in seen:
                continue
            seen.add(key)
            dte = days_to_expiration(row.expiration, today)
            in_the_money = row.strike < current
            if (
                dte is None
                or dte <= 0
                or not _qualifying_open_interest(row.call_open_interest)
                or not _passes_moneyness(in_the_money, moneyness)
            ):
                continue
            grouped.setdefault(row.expiration, []).append(
                _call_contract(row, dte, current, raw_lows, rate, in_the_money)
            )
    expirations: list[CoveredCallExpiration] = []
    for expiration, contracts in sorted(grouped.items()):
        kept = sorted(contracts, key=lambda item: item.strike_cents, reverse=True)
        expirations.append(
            CoveredCallExpiration(
                expiration=expiration,
                dte=days_to_expiration(expiration, today) or 0,
                contracts=kept,
            )
        )
    return CoveredCallPage(
        expirations=expirations,
        **_page_fields(
            chain, info, history, current, source, raw_lows, fetched_at, name, moneyness, rate
        ),
    )


def assemble_cash_secured_puts(
    chain: OptionChainResponse,
    info: StockInfoResponse | None,
    history: HistoricalResponse | None,
    today: date,
    fetched_at: datetime,
    moneyness: Moneyness = "otm",
    name: str | None = None,
    rate: Decimal | None = None,
) -> CashSecuredPutPage:
    info = info or _empty_info(chain.ticker, fetched_at)
    history = history or _empty_history(chain.ticker, fetched_at)
    current, source = current_reference(info.bid, chain.spot)
    rate = rate if rate is not None else risk_free_rate()
    raw_lows = {
        "d7": period_low(history.bars, today, 7),
        "d30": period_low(history.bars, today, 30),
        "d90": period_low(history.bars, today, 90),
        "d365": period_low(history.bars, today, 365),
    }
    grouped: dict[str, list[CashSecuredPutContract]] = {}
    seen: set[tuple[str, Decimal]] = set()
    if current is not None and chain.options_available:
        for row in chain.rows:
            key = (row.expiration, row.strike)
            if key in seen:
                continue
            seen.add(key)
            dte = days_to_expiration(row.expiration, today)
            in_the_money = row.strike > current
            if (
                dte is None
                or dte <= 0
                or not _qualifying_open_interest(row.put_open_interest)
                or not _passes_moneyness(in_the_money, moneyness)
            ):
                continue
            grouped.setdefault(row.expiration, []).append(
                _put_contract(row, dte, current, raw_lows, rate, in_the_money)
            )
    expirations: list[CashSecuredPutExpiration] = []
    for expiration, contracts in sorted(grouped.items()):
        kept = sorted(contracts, key=lambda item: item.strike_cents, reverse=True)
        expirations.append(
            CashSecuredPutExpiration(
                expiration=expiration,
                dte=days_to_expiration(expiration, today) or 0,
                contracts=kept,
            )
        )
    return CashSecuredPutPage(
        expirations=expirations,
        **_page_fields(
            chain, info, history, current, source, raw_lows, fetched_at, name, moneyness, rate
        ),
    )


def _transport_side_failure(exc: BaseException) -> bool:
    return isinstance(exc, NasdaqError) and exc.detail != "Nasdaq response is malformed"


async def _load_context(
    service: OptionChainService, ticker: Ticker, now: datetime
) -> tuple[OptionChainResponse, StockInfoResponse, HistoricalResponse]:
    today = today_new_york(now)
    history_from = (today - timedelta(days=HISTORY_LOOKBACK_DAYS)).isoformat()
    chain_result, info_result, history_result = await asyncio.gather(
        service.get_chain(ticker),
        service.get_info(ticker, now),
        service.get_history(ticker, history_from),
        return_exceptions=True,
    )
    if isinstance(chain_result, Exception):
        raise chain_result
    info = (
        _empty_info(ticker, now)
        if isinstance(info_result, NasdaqError)
        else info_result
    )
    if isinstance(info, Exception):
        raise info
    history = (
        _empty_history(ticker, now)
        if _transport_side_failure(history_result)
        else history_result
    )
    if isinstance(history, Exception):
        raise history
    return chain_result, info, history


async def load_chain(
    service: OptionChainService,
    ticker: Ticker,
    side: Side,
    now: datetime,
    moneyness: Moneyness | None = None,
    name: str | None = None,
    rate: Decimal | None = None,
) -> CoveredCallPage | CashSecuredPutPage:
    today = today_new_york(now)
    chain, info, history = await _load_context(service, ticker, now)
    if side == "put":
        return assemble_cash_secured_puts(
            chain,
            info,
            history,
            today,
            now,
            moneyness=moneyness or "otm",
            name=name,
            rate=rate,
        )
    return assemble_covered_calls(
        chain,
        info,
        history,
        today,
        now,
        moneyness=moneyness or "itm",
        name=name,
        rate=rate,
    )
