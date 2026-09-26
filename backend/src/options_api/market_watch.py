"""Shared, short-lived market odds for the chain and saved watches."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Iterable
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from options_api.contract_identity import make_watch_key, parse_watch_key
from options_api.market_calendar import (
    expiry_session_completed,
    latest_completed_session,
    quote_session,
    regular_session_open,
    session_close,
)
from options_api.market_odds import OddsEstimate, calculate_market_odds
from options_api.market_sources import DividendStatus, fetch_dividend_status, fetch_treasury_curve
from options_api.models import (
    HistoricalBar,
    MarketOddsView,
    OptionChainResponse,
    OptionQuote,
    StockInfoResponse,
)
from options_api.service import OptionChainService
from stocksweeper.storage.db import connect, rows

LOG = logging.getLogger(__name__)
_NY = ZoneInfo("America/New_York")
_REFRESH = timedelta(minutes=5)
_DIVIDEND_REFRESH = timedelta(hours=24)
_MAX_CACHED_TICKERS = 320  # All 256 watches plus recently viewed chain tickers.
_MAX_SCHEDULED = 8
_MAX_PENDING = 320
_MODEL_VERSION = "regimelib-0.1.0-market-odds-v2"
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
    "quote_bracket_missing": "Reliable call quotes do not bracket this strike",
    "quote_bounds_inconsistent": "Nearby call quotes imply contradictory odds bounds",
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
    refreshed_at: datetime | None = None
    spot: Decimal | None = None
    stock_ask: Decimal | None = None
    valuation_time: datetime | None = None
    rate_as_of_session: date | None = None
    rates: dict[str, Decimal] = field(default_factory=dict)
    entry_quotes: dict[tuple[str, str, Decimal], tuple[Decimal, Decimal]] = field(
        default_factory=dict
    )
    valid_contracts: set[tuple[str, Decimal]] = field(default_factory=set)
    invalid_contracts: set[tuple[str, Decimal]] = field(default_factory=set)


@dataclass(frozen=True)
class EntryQuote:
    spot: Decimal
    stock_ask: Decimal | None
    bid: Decimal
    ask: Decimal
    session_date: date
    source: str
    fetched_at: datetime
    rate: Decimal | None
    valuation_time: datetime
    rate_as_of_session: date | None


@dataclass(frozen=True)
class _LastGood:
    watch_key: str
    watch_created_at: datetime
    session_date: date
    fetched_at: datetime
    source: str
    call_itm_pct_tenths: int


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
    if not regular_session_open(now):
        return None, "Official completed-session close required"
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
        if not info.is_real_time:
            return None, "Underlying quote is not marked real time"
    if price is None or price <= 0 or quote_time is None:
        return None, "Underlying quote time or price is unavailable"
    age = (now - quote_time).total_seconds()
    if age < -60 or age > _REFRESH.total_seconds():
        return None, "Underlying quote is stale"
    return price, None


def _positive_close(bars: Iterable[HistoricalBar], session: date) -> Decimal | None:
    for bar in bars:
        if bar.date == session and bar.close > 0:
            return bar.close
    return None


def _probability_tenths(estimate: OddsEstimate | None) -> int | None:
    if (
        estimate is None
        or estimate.call_itm_probability is None
        or not math.isfinite(estimate.call_itm_probability)
        or not 0 <= estimate.call_itm_probability <= 1
    ):
        return None
    return int(
        (Decimal(str(estimate.call_itm_probability)) * 1000).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _entry_bid_ask(
    row: OptionQuote, side: str, spot: Decimal
) -> tuple[Decimal, Decimal] | None:
    bid = row.call_bid if side == "call" else row.put_bid
    ask = row.call_ask if side == "call" else row.put_ask
    interest = row.call_open_interest if side == "call" else row.put_open_interest
    volume = row.call_volume if side == "call" else row.put_volume
    if bid is None or ask is None or not (bid.is_finite() and ask.is_finite()):
        return None
    if bid < 0 or ask <= bid:
        return None
    if ask >= (spot if side == "call" else row.strike):
        return None
    if (interest or 0) < 5 and (volume or 0) < 5:
        return None
    if ask - bid > max(Decimal("0.25"), (bid + ask) / 2 * Decimal("0.25")):
        return None
    return bid, ask


class MarketWatchOdds:
    def __init__(
        self,
        service: OptionChainService,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime],
        *,
        data_dir: Path | None = None,
        watched_contracts: Callable[[], Iterable[tuple[str, datetime]]] | None = None,
    ) -> None:
        self.service = service
        self.client = client
        self.clock = clock
        self._data_path = data_dir / "results.duckdb" if data_dir is not None else None
        self._watched_contracts = watched_contracts
        self._watched: dict[str, datetime] = {}
        self._last_good: dict[str, _LastGood] = {}
        self._last_good_session: date | None = None
        self._cache: dict[str, _Snapshot] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, None] = {}
        self._chain_refresh_attempts: dict[str, tuple[datetime, str, datetime]] = {}
        self._dividends: dict[str, DividendStatus] = {}
        self._semaphore = asyncio.Semaphore(2)
        self._closed = False
        if self._data_path is not None:
            with connect(self._data_path) as connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS market_odds_last_good (
                         watch_key VARCHAR NOT NULL,
                         watch_created_at TIMESTAMPTZ NOT NULL,
                         model_version VARCHAR NOT NULL,
                         session_date DATE NOT NULL,
                         fetched_at TIMESTAMPTZ NOT NULL,
                         source VARCHAR NOT NULL,
                         call_itm_pct_tenths INTEGER NOT NULL
                             CHECK (call_itm_pct_tenths BETWEEN 0 AND 1000),
                         PRIMARY KEY (watch_key, model_version)
                       )"""
                )
            self._sync_watches(_as_utc(self.clock()))

    def _sync_watches(self, now: datetime) -> set[str]:
        if self._data_path is None or self._watched_contracts is None:
            return set()
        watched = {key: _as_utc(created) for key, created in self._watched_contracts()}
        completed = latest_completed_session(now)
        if watched == self._watched and completed == self._last_good_session:
            return set()
        changed = {
            parsed[0]
            for key in watched.keys() | self._watched.keys()
            if watched.get(key) != self._watched.get(key)
            if (parsed := parse_watch_key(key)) is not None
        }
        self._watched = watched
        self._last_good_session = completed
        with connect(self._data_path) as connection:
            connection.execute(
                """DELETE FROM market_odds_last_good
                   WHERE model_version <> ? OR session_date < ?
                      OR NOT EXISTS (
                        SELECT 1 FROM watches
                        WHERE watches.watch_key = market_odds_last_good.watch_key
                          AND watches.created_at = market_odds_last_good.watch_created_at
                      )""",
                [_MODEL_VERSION, completed],
            )
            saved = rows(
                connection,
                """SELECT watch_key, watch_created_at, session_date, fetched_at,
                          source, call_itm_pct_tenths
                   FROM market_odds_last_good WHERE model_version = ?""",
                [_MODEL_VERSION],
            )
        self._last_good = {}
        for row in saved:
            key = row["watch_key"]
            created = row["watch_created_at"]
            probability = row["call_itm_pct_tenths"]
            if (
                not isinstance(key, str)
                or not isinstance(created, datetime)
                or self._watched.get(key) != _as_utc(created)
                or row["source"] not in ("nasdaq", "yahoo")
                or not isinstance(probability, int)
                or not 0 <= probability <= 1000
            ):
                continue
            self._last_good[key] = _LastGood(
                key,
                _as_utc(created),
                row["session_date"],
                _as_utc(row["fetched_at"]),
                row["source"],
                probability,
            )
        return changed

    def _persist_current_watches(self, ticker: str, snapshot: _Snapshot) -> None:
        if self._data_path is None or snapshot.error is not None or snapshot.source is None:
            return
        values: list[tuple[object, ...]] = []
        for key, created in self._watched.items():
            identity = parse_watch_key(key)
            if identity is None:
                continue
            symbol, root, _, expiry, strike = identity
            if (
                symbol != ticker or root != ticker or expiry in snapshot.reasons
                or (expiry, strike) not in snapshot.valid_contracts
            ):
                continue
            call_itm = _probability_tenths(snapshot.odds.get((expiry, strike)))
            if call_itm is None:
                continue
            existing = self._last_good.get(key)
            if (
                existing is not None
                and existing.watch_created_at == created
                and existing.session_date >= snapshot.session_date
                and existing.fetched_at >= snapshot.fetched_at
            ):
                continue
            values.append((
                key, created, _MODEL_VERSION, snapshot.session_date,
                snapshot.fetched_at, snapshot.source, call_itm,
            ))
        if not values:
            return
        try:
            with connect(self._data_path) as connection:
                connection.executemany(
                    """INSERT INTO market_odds_last_good
                       (watch_key, watch_created_at, model_version, session_date,
                        fetched_at, source, call_itm_pct_tenths)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (watch_key, model_version) DO UPDATE SET
                         watch_created_at = excluded.watch_created_at,
                         session_date = excluded.session_date,
                         fetched_at = excluded.fetched_at,
                         source = excluded.source,
                         call_itm_pct_tenths = excluded.call_itm_pct_tenths
                       WHERE market_odds_last_good.session_date < excluded.session_date
                          OR (market_odds_last_good.session_date = excluded.session_date
                              AND market_odds_last_good.fetched_at <= excluded.fetched_at)""",
                    values,
                )
            for key, created, _, session, fetched, source, call_itm in values:
                self._last_good[key] = _LastGood(
                    key, created, session, fetched, source, call_itm
                )
        except Exception:
            LOG.exception("failed to persist last available market odds for %s", ticker)

    def _remember(self, ticker: str, snapshot: _Snapshot) -> None:
        if snapshot.refreshed_at is None:
            snapshot.refreshed_at = _as_utc(self.clock())
        self._cache.pop(ticker, None)
        self._cache[ticker] = snapshot
        while len(self._cache) > _MAX_CACHED_TICKERS:
            oldest = next(iter(self._cache))
            self._cache.pop(oldest)
            self._chain_refresh_attempts.pop(oldest, None)
        self._persist_current_watches(ticker, snapshot)

    def _valid(self, entry: _Snapshot, now: datetime) -> bool:
        if entry.error is not None:
            return now - (entry.refreshed_at or entry.fetched_at) < _REFRESH
        if regular_session_open(now):
            return entry.session_date == quote_session(now) and now - entry.fetched_at < _REFRESH
        completed = latest_completed_session(now)
        return (
            entry.session_date == completed
            and entry.valuation_time is not None
            and entry.valuation_time >= session_close(completed)
        )

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
        call_itm = _probability_tenths(estimate)
        if call_itm is None:
            return MarketOddsView(
                status="unavailable",
                reason=_REASONS.get(estimate.reason or "", estimate.reason or "Odds unavailable"),
                **common,
            )
        itm = call_itm if side == "call" else 1000 - call_itm
        return MarketOddsView(
            status="available", itm_pct_tenths=itm, otm_pct_tenths=1000 - itm, **common
        )

    def lookup_last_good(
        self, ticker: str, side: str, expiry: str, strike: Decimal, root: str | None = None
    ) -> MarketOddsView | None:
        if root is not None and root != ticker:
            return None
        now = _as_utc(self.clock())
        try:
            if expiry_session_completed(date.fromisoformat(expiry), now):
                return None
        except ValueError:
            return None
        key = make_watch_key(ticker, root or ticker, side, expiry, strike)
        saved = self._last_good.get(key)
        if (
            saved is None
            or self._watched.get(key) != saved.watch_created_at
            or saved.session_date != latest_completed_session(now)
        ):
            return None
        entry = self._cache.get(ticker)
        if entry is not None:
            estimate = entry.odds.get((expiry, strike))
            if (expiry, strike) in entry.invalid_contracts or (
                estimate is not None
                and estimate.reason in ("invalid_contract", "duplicate_contract")
            ):
                return None
        itm = (
            saved.call_itm_pct_tenths if side == "call"
            else 1000 - saved.call_itm_pct_tenths
        )
        return MarketOddsView(
            status="available",
            itm_pct_tenths=itm,
            otm_pct_tenths=1000 - itm,
            source=saved.source,
            fetched_at=saved.fetched_at,
            session_date=saved.session_date,
            model_version=_MODEL_VERSION,
        )

    def rate_for(self, ticker: str, expiry: str) -> tuple[Decimal, datetime] | None:
        entry = self._cache.get(ticker)
        if (
            entry is None
            or not self._valid(entry, _as_utc(self.clock()))
            or entry.error is not None
            or entry.valuation_time is None
            or entry.rate_as_of_session is None
            or expiry not in entry.rates
        ):
            return None
        return entry.rates[expiry], entry.valuation_time

    def entry_quote(
        self, ticker: str, side: str, expiry: str, strike: Decimal, root: str | None = None
    ) -> EntryQuote | None:
        if root is not None and root != ticker:
            return None
        entry = self._cache.get(ticker)
        if (
            entry is None
            or not self._valid(entry, _as_utc(self.clock()))
            or entry.error is not None
            or entry.spot is None
            or entry.source is None
            or entry.valuation_time is None
        ):
            return None
        quote = entry.entry_quotes.get((side, expiry, strike))
        if quote is None:
            return None
        return EntryQuote(
            entry.spot,
            entry.stock_ask,
            quote[0],
            quote[1],
            entry.session_date,
            entry.source,
            entry.fetched_at,
            entry.rates.get(expiry),
            entry.valuation_time,
            entry.rate_as_of_session,
        )

    def schedule_for_chain(
        self, ticker: str, chain_fetched_at: datetime, chain_source: str
    ) -> bool:
        """Refresh a displayed chain when its quotes differ from the odds snapshot."""
        if self._closed:
            return False
        now = _as_utc(self.clock())
        target = _as_utc(chain_fetched_at)
        entry = self._cache.get(ticker)
        if (
            entry is not None
            and self._valid(entry, now)
            and entry.error is None
            and entry.fetched_at == target
            and entry.source == chain_source
        ):
            return True
        if ticker in self._tasks or ticker in self._pending:
            return False
        if entry is not None and entry.error is not None and self._valid(entry, now):
            return False
        previous = self._chain_refresh_attempts.get(ticker)
        if previous is not None and now - previous[2] < _REFRESH:
            prior_target, prior_source, _ = previous
            if prior_source == chain_source and (
                prior_target == target or entry is None or entry.source != chain_source
            ):
                return False
        if len(self._pending) >= _MAX_PENDING:
            return False
        self._chain_refresh_attempts[ticker] = target, chain_source, now
        self._pending[ticker] = None
        self._pump()
        return False

    def schedule(self, tickers: Iterable[str]) -> None:
        if self._closed:
            return
        now = _as_utc(self.clock())
        changed = self._sync_watches(now)
        for ticker in sorted(set(tickers)):
            current = self._cache.get(ticker)
            if current is not None and self._valid(current, now):
                self._persist_current_watches(ticker, current)
            if (
                ticker in self._tasks
                or ticker in self._pending
                or (ticker not in changed and current is not None and self._valid(current, now))
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

    async def _completed_session_close(self, ticker: str, now: datetime) -> Decimal | None:
        session = latest_completed_session(now)
        from_date = (session - timedelta(days=10)).isoformat()
        try:
            history = await self.service.get_history(ticker, from_date)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("completed-session close unavailable for %s", ticker)
            return None
        close = _positive_close(history.bars, session)
        if close is None:
            # A daily bar can appear after the first post-close fetch. Do not
            # keep that incomplete window for the history cache's full day.
            self.service.release_history(ticker, from_date)
        return close

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
                valuation_time = now if regular_session_open(now) else session_close(session)
                if quote_session(chain.fetched_at) != session:
                    self._remember(ticker, _Snapshot(
                        chain.fetched_at, session, chain.source, {}, {},
                        "Option chain belongs to another quote session",
                    ))
                    return
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
                if regular_session_open(now):
                    spot, spot_reason = _spot(chain, info, now)
                    stock_ask = (
                        info.ask if chain.source == "nasdaq" and spot_reason is None else None
                    )
                else:
                    # Last-sale time does not timestamp the bid and ask. Use
                    # only the exact official bar for the completed session.
                    close = await self._completed_session_close(ticker, now)
                    if close is not None:
                        spot, spot_reason = close, None
                    else:
                        spot, spot_reason = None, "Official completed-session close is unavailable"
                    stock_ask = None
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
                rates: dict[str, Decimal] = {}
                for expiry_text in {row.expiration for row in chain.rows}:
                    expiry = date.fromisoformat(expiry_text)
                    if expiry <= valuation_time.astimezone(_NY).date():
                        reasons[expiry_text] = "Same-day option quote timing cannot be verified"
                    elif (
                        curve is None
                        or (rate := curve.rate_for(expiry, valuation_time)) is None
                        or not math.isfinite(rate)
                        or not 0 <= rate <= 0.25
                    ):
                        reasons[expiry_text] = "Dated Treasury rate is unavailable"
                    else:
                        eligible, reason = dividends.eligible_for(expiry)
                        if eligible:
                            allowed.add(expiry_text)
                            rates[expiry_text] = Decimal(str(rate))
                        else:
                            reasons[expiry_text] = reason or "Dividend exposure is unsupported"
                by_contract: dict[tuple[str, Decimal], list[OptionQuote]] = defaultdict(list)
                for row in chain.rows:
                    by_contract[(row.expiration, row.strike)].append(row)
                standard_rows = {
                    key: matches[0]
                    for key, matches in by_contract.items()
                    if len(matches) == 1
                    and matches[0].identity_reason is None
                    and matches[0].root == ticker
                }
                entry_quotes: dict[tuple[str, str, Decimal], tuple[Decimal, Decimal]] = {}
                for (expiry, strike), row in standard_rows.items():
                    for side in ("call", "put"):
                        quote = _entry_bid_ask(row, side, spot)
                        if quote is not None:
                            entry_quotes[(side, expiry, strike)] = quote
                odds: dict[tuple[str, Decimal], OddsEstimate] = {}
                if allowed and curve is not None and spot is not None:
                    odds = await asyncio.to_thread(
                        calculate_market_odds,
                        chain.rows,
                        spot,
                        lambda expiry: float(rates[expiry.isoformat()]),
                        allowed,
                        valuation_time,
                    )
                self._remember(ticker, _Snapshot(
                    chain.fetched_at, session, chain.source, odds, reasons,
                    spot=spot, stock_ask=stock_ask, valuation_time=valuation_time,
                    rate_as_of_session=curve.as_of if curve is not None else None,
                    rates=rates, entry_quotes=entry_quotes,
                    valid_contracts=set(standard_rows),
                    invalid_contracts=set(by_contract) - set(standard_rows),
                ))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("market odds refresh failed for %s", ticker)
                self._remember(ticker, _Snapshot(
                    now, quote_session(now), None, {}, {}, "Market data or pricing is unavailable"
                ))
