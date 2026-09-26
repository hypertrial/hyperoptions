"""Shared, short-lived market odds for the chain and saved watches."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import httpx

from options_api.market_calendar import (
    latest_completed_session,
    quote_session,
    regular_session_open,
    session_close,
)
from options_api.market_odds import OddsEstimate, calculate_market_odds
from options_api.market_sources import DividendStatus, fetch_dividend_status, fetch_treasury_curve
from options_api.models import MarketOddsView, OptionChainResponse, StockInfoResponse
from options_api.service import OptionChainService

LOG = logging.getLogger(__name__)
_NY = ZoneInfo("America/New_York")
_REFRESH = timedelta(minutes=5)
_DIVIDEND_REFRESH = timedelta(hours=24)
_MAX_CACHED_TICKERS = 320  # All 256 watches plus recently viewed chain tickers.
_MAX_SCHEDULED = 8
_MAX_PENDING = 320
_MODEL_VERSION = "regimelib-0.1.0-market-odds-v1"
_REASONS = {
    "same_day": "Same-day option quote timing cannot be verified",
    "same_day_quote_timing": "Same-day option quote timing cannot be verified",
    "invalid_contract": "Contract terms cannot be verified",
    "duplicate_contract": "Contract appears more than once in the chain",
    "invalid_expiration": "Contract expiration is invalid",
    "expired": "Expiry session has completed",
    "invalid_spot": "Underlying price is unavailable",
    "invalid_quote": "Option quote is invalid or too wide",
    "rate_unavailable": "Dated Treasury rate is unavailable",
    "insufficient_quotes": "Too few reliable nearby option quotes",
    "calibration_failed": "Market model did not fit quoted prices",
    "quote_bounds_wide": "Quoted prices do not bound these odds narrowly enough",
    "quote_bounds_mismatch": "Model odds conflict with nearby option quotes",
    "quote_fit_failed": "Model price differs from the quoted market",
    "numerical_unstable": "Pricing calculation did not converge",
    "nonmonotone_odds": "Model odds are not monotone across strikes",
    "pricing_budget_exceeded": "Too many contracts to price in this snapshot",
}


@dataclass
class _Snapshot:
    fetched_at: datetime
    session_date: date
    source: str | None
    odds: dict[tuple[str, Decimal], OddsEstimate]
    reasons: dict[str, str]
    error: str | None = None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _underlying_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%b %d, %Y %I:%M %p ET").replace(
            tzinfo=_NY
        ).astimezone(UTC)
    except ValueError:
        return None


def _spot(
    chain: OptionChainResponse, info: StockInfoResponse, now: datetime
) -> tuple[Decimal | None, str | None]:
    if chain.source == "yahoo":
        price = chain.spot
        quote_time = _underlying_time(chain.last_trade_timestamp)
    else:
        bid, ask = info.bid, info.ask
        if (
            bid is None
            or ask is None
            or bid <= 0
            or ask < bid
            or (ask - bid) / ((bid + ask) / 2) > Decimal("0.02")
            or abs((chain.fetched_at - info.fetched_at).total_seconds()) > 120
        ):
            return None, "A coherent underlying bid and ask is unavailable"
        price = (bid + ask) / 2
        quote_time = _underlying_time(info.quote_timestamp)
        if regular_session_open(now) and not info.is_real_time:
            return None, "Underlying quote is not marked real time"
    if price is None or price <= 0 or quote_time is None:
        return None, "Underlying quote time or price is unavailable"
    if regular_session_open(now):
        age = (now - quote_time).total_seconds()
        if age < -60 or age > _REFRESH.total_seconds():
            return None, "Underlying quote is stale"
    else:
        completed = latest_completed_session(now)
        if quote_session(quote_time) != completed or quote_time > session_close(completed):
            return None, "Last regular-session quote cannot be verified"
    return price, None


class MarketWatchOdds:
    def __init__(
        self,
        service: OptionChainService,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime],
    ) -> None:
        self.service = service
        self.client = client
        self.clock = clock
        self._cache: dict[str, _Snapshot] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, None] = {}
        self._dividends: dict[str, DividendStatus] = {}
        self._semaphore = asyncio.Semaphore(2)
        self._closed = False

    def _remember(self, ticker: str, snapshot: _Snapshot) -> None:
        self._cache.pop(ticker, None)
        self._cache[ticker] = snapshot
        while len(self._cache) > _MAX_CACHED_TICKERS:
            self._cache.pop(next(iter(self._cache)))

    def _valid(self, entry: _Snapshot, now: datetime) -> bool:
        if entry.error is not None:
            return now - entry.fetched_at < _REFRESH
        if regular_session_open(now):
            return entry.session_date == quote_session(now) and now - entry.fetched_at < _REFRESH
        return entry.session_date == latest_completed_session(now)

    def lookup(
        self, ticker: str, side: str, expiry: str, strike: Decimal, root: str | None = None
    ) -> MarketOddsView:
        if root is not None and root != ticker:
            return MarketOddsView(status="unavailable", reason="Contract terms cannot be verified")
        entry = self._cache.get(ticker)
        now = _as_utc(self.clock())
        if entry is None or not self._valid(entry, now):
            return MarketOddsView(status="pending", reason="Refreshing market odds")
        common = {
            "source": entry.source,
            "fetched_at": entry.fetched_at,
            "session_date": entry.session_date,
            "model_version": _MODEL_VERSION,
        }
        if entry.error:
            return MarketOddsView(status="unavailable", reason=entry.error, **common)
        if expiry in entry.reasons:
            return MarketOddsView(status="unavailable", reason=entry.reasons[expiry], **common)
        estimate = entry.odds.get((expiry, strike))
        if estimate is None:
            return MarketOddsView(
                status="unavailable",
                reason="Contract is absent from the current option chain",
                **common,
            )
        if (
            estimate.call_itm_probability is None
            or not math.isfinite(estimate.call_itm_probability)
            or not 0 <= estimate.call_itm_probability <= 1
        ):
            return MarketOddsView(
                status="unavailable",
                reason=_REASONS.get(estimate.reason or "", estimate.reason or "Odds unavailable"),
                **common,
            )
        call_itm = int(
            (Decimal(str(estimate.call_itm_probability)) * 1000).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        itm = call_itm if side == "call" else 1000 - call_itm
        return MarketOddsView(
            status="available", itm_pct_tenths=itm, otm_pct_tenths=1000 - itm, **common
        )

    def schedule(self, tickers: Iterable[str]) -> None:
        if self._closed:
            return
        now = _as_utc(self.clock())
        for ticker in sorted(set(tickers)):
            current = self._cache.get(ticker)
            if (
                ticker in self._tasks
                or ticker in self._pending
                or (current is not None and self._valid(current, now))
            ):
                continue
            if len(self._pending) >= _MAX_PENDING:
                self._remember(
                    ticker,
                    _Snapshot(now, quote_session(now), None, {}, {}, "Market odds queue is busy"),
                )
                continue
            self._pending[ticker] = None
        self._pump()

    def _pump(self) -> None:
        while not self._closed and self._pending and len(self._tasks) < _MAX_SCHEDULED:
            ticker = next(iter(self._pending))
            self._pending.pop(ticker)
            task = asyncio.create_task(self._refresh(ticker))
            self._tasks[ticker] = task
            task.add_done_callback(lambda _task, symbol=ticker: self._task_done(symbol))

    def _task_done(self, ticker: str) -> None:
        self._tasks.pop(ticker, None)
        self._pump()

    async def close(self) -> None:
        self._closed = True
        self._pending.clear()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _dividend_status(self, ticker: str, now: datetime) -> DividendStatus:
        cached = self._dividends.get(ticker)
        ttl = _REFRESH if cached is not None and cached.kind == "unknown" else _DIVIDEND_REFRESH
        if cached is not None and now - cached.as_of < ttl:
            return cached
        status = await fetch_dividend_status(ticker, now)
        self._dividends[ticker] = status
        if len(self._dividends) > _MAX_CACHED_TICKERS:
            self._dividends.pop(next(iter(self._dividends)))
        return status

    async def _refresh(self, ticker: str) -> None:
        async with self._semaphore:
            now = _as_utc(self.clock())
            try:
                chain_result, info_result = await asyncio.gather(
                    self.service.get_chain(ticker),
                    self.service.get_info(ticker, now),
                    return_exceptions=True,
                )
                if isinstance(chain_result, BaseException):
                    raise chain_result
                chain = chain_result
                # A full-chain fallback can take over a minute. Assess quote
                # freshness and expiry against the time it actually completes.
                now = _as_utc(self.clock())
                if isinstance(info_result, BaseException):
                    if chain.source != "yahoo":
                        raise info_result
                    info = StockInfoResponse(
                        ticker=ticker,
                        fetched_at=now,
                        from_cache=False,
                        bid=None,
                        ask=None,
                        quote_timestamp=None,
                        is_real_time=False,
                        market_session=None,
                    )
                else:
                    info = info_result
                session = quote_session(now)
                if chain.truncated:
                    self._remember(ticker, _Snapshot(
                        chain.fetched_at,
                        session,
                        chain.source,
                        {},
                        {},
                        "Option-chain coverage is incomplete",
                    ))
                    return
                spot, spot_reason = _spot(chain, info, now)
                if spot_reason is not None:
                    self._remember(ticker, _Snapshot(
                        chain.fetched_at, session, chain.source, {}, {}, spot_reason
                    ))
                    return
                curve, dividends = await asyncio.gather(
                    fetch_treasury_curve(self.client, now), self._dividend_status(ticker, now)
                )
                reasons: dict[str, str] = {}
                allowed: set[str] = set()
                for expiry_text in {row.expiration for row in chain.rows}:
                    expiry = date.fromisoformat(expiry_text)
                    if expiry <= now.astimezone(_NY).date():
                        reasons[expiry_text] = "Same-day option quote timing cannot be verified"
                    elif (
                        curve is None
                        or (rate := curve.rate_for(expiry, now)) is None
                        or rate < 0
                    ):
                        reasons[expiry_text] = "Dated Treasury rate is unavailable"
                    else:
                        eligible, reason = dividends.eligible_for(expiry)
                        if eligible:
                            allowed.add(expiry_text)
                        else:
                            reasons[expiry_text] = reason or "Dividend exposure is unsupported"
                odds: dict[tuple[str, Decimal], OddsEstimate] = {}
                if allowed and curve is not None and spot is not None:
                    odds = await asyncio.to_thread(
                        calculate_market_odds,
                        chain.rows,
                        spot,
                        lambda expiry: curve.rate_for(expiry, now),
                        allowed,
                        now,
                    )
                self._remember(ticker, _Snapshot(
                    chain.fetched_at, session, chain.source, odds, reasons
                ))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("market odds refresh failed for %s", ticker)
                self._remember(ticker, _Snapshot(
                    now, quote_session(now), None, {}, {}, "Market data or pricing is unavailable"
                ))
