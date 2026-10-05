"""Shared, short-lived market odds for the chain and saved watches."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from pathlib import Path

import httpx

from options_api.contract_identity import make_watch_key, parse_watch_key
from options_api.market_calendar import (
    expiry_session_completed,
    latest_completed_session,
    quote_session,
    regular_session_open,
    session_close,
)
from options_api.market_curve_shadow import CurveShadowResult, calculate_curve_shadow
from options_api.market_odds import OddsEstimate, calculate_market_odds
from options_api.market_sources import (
    DividendStatus,
    InputAcquisitionError,
    TreasuryCurve,
    fetch_dividend_status,
    fetch_treasury_curve,
)
from options_api.models import (
    HistoricalResponse,
    IvSpotBasis,
    MarketOddsView,
    OptionChainResponse,
    OptionQuote,
    StockInfoResponse,
)
from options_api.pricing_context import (
    EntryQuote,
    PricingContext,
    PricingIssue,
    PricingSelection,
    build_pricing_context,
    displayed_chain_reason,
    positive_close,
    pricing_block_reason,
    pricing_issue,
)
from options_api.service import OptionChainService
from stocksweeper.storage.db import connect, rows

LOG = logging.getLogger(__name__)
_REFRESH = timedelta(minutes=5)
PAGE_INPUT_TIMEOUT = 2.0
_InputState = Literal["done", "pending", "failed"]


@dataclass(frozen=True)
class _PageInputs:
    curve: TreasuryCurve | None
    curve_state: _InputState
    dividends: DividendStatus
    dividend_state: _InputState
_DIVIDEND_REFRESH = timedelta(hours=24)
_MAX_CACHED_TICKERS = 320  # All 256 watches plus recently viewed chain tickers.
_MAX_SCHEDULED = 8
_MAX_PENDING = 320
_MODEL_VERSION = "regimelib-0.1.0-market-odds-v2"
_CURVE_VERSION = "constrained-call-curve-shadow-v2"
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
    underlying_quote_time: datetime | None = None
    underlying_quote_fetched_at: datetime | None = None
    rate_as_of_session: date | None = None
    rates: dict[str, Decimal] = field(default_factory=dict)
    entry_quotes: dict[tuple[str, str, Decimal], tuple[Decimal, Decimal]] = field(
        default_factory=dict
    )
    valid_contracts: set[tuple[str, Decimal]] = field(default_factory=set)
    invalid_contracts: set[tuple[str, Decimal]] = field(default_factory=set)
    spot_basis: IvSpotBasis | None = None
    quote_issues: dict[tuple[str, str, Decimal], PricingIssue] = field(default_factory=dict)
    identity_issues: dict[tuple[str, Decimal], PricingIssue] = field(default_factory=dict)


@dataclass(frozen=True)
class UnderlyingQuote:
    spot: Decimal
    session_date: date
    source: str
    fetched_at: datetime
    quote_time: datetime


@dataclass(frozen=True)
class _LastGood:
    watch_key: str
    watch_created_at: datetime
    session_date: date
    fetched_at: datetime
    source: str
    call_itm_pct_tenths: int
    call_bound_low_pct_tenths: int | None
    call_bound_high_pct_tenths: int | None
    quote_support_score: int | None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _probability_tenths(estimate: OddsEstimate | None) -> int | None:
    if (
        estimate is None
        or estimate.call_itm_probability is None
        or not math.isfinite(estimate.call_itm_probability)
        or not 0 <= estimate.call_itm_probability <= 1
    ):
        return None
    return _to_tenths(estimate.call_itm_probability)


def _to_tenths(value: float) -> int:
    return int((Decimal(str(value)) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _quote_support(estimate: OddsEstimate, side: str) -> tuple[int | None, int | None, int | None]:
    if estimate.bounds is None:
        return None, None, None
    lower, upper = estimate.bounds
    if not (math.isfinite(lower) and math.isfinite(upper) and 0 <= lower <= upper <= 1):
        return None, None, None
    low, high = (1 - upper, 1 - lower) if side == "put" else (lower, upper)
    score = int(
        (Decimal(str(1 - (upper - lower))) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    return _to_tenths(low), _to_tenths(high), score


class MarketWatchOdds:
    def __init__(
        self,
        service: OptionChainService,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime],
        *,
        data_dir: Path | None = None,
        watched_contracts: Callable[[], Iterable[tuple[str, datetime]]] | None = None,
        dividend_provider: Callable[[str, datetime], Awaitable[DividendStatus]] | None = None,
    ) -> None:
        self.service = service
        self.client = client
        self.clock = clock
        self._data_path = data_dir / "results.duckdb" if data_dir is not None else None
        self._watched_contracts = watched_contracts
        self._dividend_provider = dividend_provider
        self._watched: dict[str, datetime] = {}
        self._last_good: dict[str, _LastGood] = {}
        self._last_good_session: date | None = None
        self._cache: dict[str, _Snapshot] = {}
        self._curve_shadow: dict[str, dict[str, object]] = {}
        self._curve_results: dict[str, tuple[_Snapshot, CurveShadowResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._shadow_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[str, None] = {}
        self._chain_refresh_attempts: dict[str, tuple[datetime, str, datetime]] = {}
        self._dividends: dict[str, DividendStatus] = {}
        self._page_inputs: set[asyncio.Task[object]] = set()
        self._page_input_flights: dict[str, asyncio.Task[object]] = {}
        self._page_input_components: dict[
            asyncio.Task[object], tuple[asyncio.Task, asyncio.Task]
        ] = {}
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
                         call_bound_low_pct_tenths INTEGER,
                         call_bound_high_pct_tenths INTEGER,
                         quote_support_score INTEGER,
                         PRIMARY KEY (watch_key, model_version)
                       )"""
                )
                for column in (
                    "call_bound_low_pct_tenths",
                    "call_bound_high_pct_tenths",
                    "quote_support_score",
                ):
                    connection.execute(
                        "ALTER TABLE market_odds_last_good "
                        f"ADD COLUMN IF NOT EXISTS {column} INTEGER"
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
                          source, call_itm_pct_tenths, call_bound_low_pct_tenths,
                          call_bound_high_pct_tenths, quote_support_score
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
            low = row["call_bound_low_pct_tenths"]
            high = row["call_bound_high_pct_tenths"]
            score = row["quote_support_score"]
            if not (
                isinstance(low, int)
                and isinstance(high, int)
                and isinstance(score, int)
                and 0 <= low <= high <= 1000
                and 0 <= score <= 100
            ):
                low = high = score = None
            self._last_good[key] = _LastGood(
                key,
                _as_utc(created),
                row["session_date"],
                _as_utc(row["fetched_at"]),
                row["source"],
                probability,
                low,
                high,
                score,
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
                symbol != ticker
                or root != ticker
                or expiry in snapshot.reasons
                or (expiry, strike) not in snapshot.valid_contracts
            ):
                continue
            estimate = snapshot.odds.get((expiry, strike))
            call_itm = _probability_tenths(estimate)
            if call_itm is None:
                continue
            bound_low, bound_high, score = _quote_support(estimate, "call")
            existing = self._last_good.get(key)
            if (
                existing is not None
                and existing.watch_created_at == created
                and existing.session_date >= snapshot.session_date
                and existing.fetched_at >= snapshot.fetched_at
            ):
                continue
            values.append(
                (
                    key,
                    created,
                    _MODEL_VERSION,
                    snapshot.session_date,
                    snapshot.fetched_at,
                    snapshot.source,
                    call_itm,
                    bound_low,
                    bound_high,
                    score,
                )
            )
        if not values:
            return
        try:
            with connect(self._data_path) as connection:
                connection.executemany(
                    """INSERT INTO market_odds_last_good
                       (watch_key, watch_created_at, model_version, session_date,
                        fetched_at, source, call_itm_pct_tenths,
                        call_bound_low_pct_tenths, call_bound_high_pct_tenths,
                        quote_support_score)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (watch_key, model_version) DO UPDATE SET
                         watch_created_at = excluded.watch_created_at,
                         session_date = excluded.session_date,
                         fetched_at = excluded.fetched_at,
                         source = excluded.source,
                         call_itm_pct_tenths = excluded.call_itm_pct_tenths,
                         call_bound_low_pct_tenths = excluded.call_bound_low_pct_tenths,
                         call_bound_high_pct_tenths = excluded.call_bound_high_pct_tenths,
                         quote_support_score = excluded.quote_support_score
                       WHERE market_odds_last_good.session_date < excluded.session_date
                          OR (market_odds_last_good.session_date = excluded.session_date
                              AND market_odds_last_good.fetched_at <= excluded.fetched_at)""",
                    values,
                )
            for key, created, _, session, fetched, source, call_itm, low, high, score in values:
                self._last_good[key] = _LastGood(
                    key, created, session, fetched, source, call_itm, low, high, score
                )
        except Exception:
            LOG.exception("failed to persist last available market odds for %s", ticker)

    def _remember(self, ticker: str, snapshot: _Snapshot) -> None:
        if snapshot.refreshed_at is None:
            snapshot.refreshed_at = _as_utc(self.clock())
        self._cache.pop(ticker, None)
        self._curve_shadow.pop(ticker, None)
        self._curve_results.pop(ticker, None)
        self._cache[ticker] = snapshot
        while len(self._cache) > _MAX_CACHED_TICKERS:
            oldest = next(iter(self._cache))
            self._cache.pop(oldest)
            self._chain_refresh_attempts.pop(oldest, None)
            self._curve_results.pop(oldest, None)
        self._persist_current_watches(ticker, snapshot)

    def curve_shadow_report(self, ticker: str) -> dict[str, object] | None:
        """Research-only report; it never supplies published market odds."""
        return self._curve_shadow.get(ticker)

    def _record_curve_shadow(
        self,
        ticker: str,
        snapshot: _Snapshot,
        shadow: CurveShadowResult,
        benchmark_ms: float,
        live_refresh_ms: float,
    ) -> None:
        bands: dict[str, dict[str, dict[str, int]]] = {}
        for (expiry, strike), benchmark in snapshot.odds.items():
            if (expiry, strike) not in snapshot.valid_contracts or snapshot.spot is None:
                continue
            ratio = float(strike / snapshot.spot)
            band = (
                "near_atm"
                if 0.95 <= ratio <= 1.05
                else ("moderate" if 0.85 <= ratio <= 1.15 else "tail")
            )
            counts = bands.setdefault(expiry, {}).setdefault(
                band, {"contracts": 0, "benchmark_available": 0, "shadow_available": 0}
            )
            counts["contracts"] += 1
            counts["benchmark_available"] += int(benchmark.call_itm_probability is not None)
            candidate = shadow.odds.get((expiry, strike))
            counts["shadow_available"] += int(
                candidate is not None and candidate.call_itm_probability is not None
            )
        report: dict[str, object] = {
            "model_version": _CURVE_VERSION,
            "held_out_inside": shadow.held_out_inside,
            "held_out_count": shadow.held_out_count,
            "shadow_held_out_predicted": shadow.shadow_held_out_predicted,
            "benchmark_held_out_inside": shadow.benchmark_held_out_inside,
            "benchmark_held_out_predicted": shadow.benchmark_held_out_predicted,
            "paired_held_out_count": shadow.paired_held_out_count,
            "paired_shadow_inside": shadow.paired_shadow_inside,
            "paired_benchmark_inside": shadow.paired_benchmark_inside,
            "held_out_cohort": shadow.held_out_cohort,
            "held_out_comparison_ready": (
                shadow.held_out_cohort == "benchmark_as_fitted"
                and shadow.held_out_count > 0
                and shadow.paired_held_out_count == shadow.held_out_count
            ),
            "elapsed_ms": shadow.elapsed_ms,
            "benchmark_ms": benchmark_ms,
            "live_refresh_ms": live_refresh_ms,
            "rejection_reasons": shadow.rejection_reasons,
            "by_expiry_moneyness": bands,
        }
        self._curve_shadow[ticker] = report
        self._curve_results[ticker] = snapshot, shadow
        if self._data_path is not None and snapshot.source is not None:
            with connect(self._data_path) as connection:
                connection.execute(
                    """INSERT INTO market_curve_shadow_runs
                       (ticker, chain_fetched_at, source, session_date,
                        model_version, report_json) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT DO NOTHING""",
                    [
                        ticker,
                        snapshot.fetched_at,
                        snapshot.source,
                        snapshot.session_date,
                        _CURVE_VERSION,
                        json.dumps(report, sort_keys=True),
                    ],
                )

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
        bound_low, bound_high, support = _quote_support(estimate, side)
        return MarketOddsView(
            status="available",
            itm_pct_tenths=itm,
            otm_pct_tenths=1000 - itm,
            bound_low_pct_tenths=bound_low,
            bound_high_pct_tenths=bound_high,
            quote_support_score=support,
            **common,
        )

    def lookup_curve(
        self, ticker: str, side: str, expiry: str, strike: Decimal, root: str | None = None
    ) -> MarketOddsView:
        """Read only the result of the bounded background curve fit."""
        common = {"method": "constrained_call_curve", "model_version": _CURVE_VERSION}
        if root is not None and root != ticker:
            return MarketOddsView(
                status="unavailable", reason="Contract terms cannot be verified", **common
            )
        snapshot = self._cache.get(ticker)
        if snapshot is None or not self._valid(snapshot, _as_utc(self.clock())):
            return MarketOddsView(
                status="pending", reason="Refreshing option quote snapshot", **common
            )
        common.update(
            source=snapshot.source,
            fetched_at=snapshot.fetched_at,
            session_date=snapshot.session_date,
        )
        if snapshot.error or expiry in snapshot.reasons:
            return MarketOddsView(
                status="unavailable", reason=snapshot.error or snapshot.reasons[expiry], **common
            )
        if (expiry, strike) not in snapshot.valid_contracts:
            return MarketOddsView(
                status="unavailable", reason="Contract terms cannot be verified", **common
            )
        saved = self._curve_results.get(ticker)
        if saved is None or saved[0] is not snapshot:
            return MarketOddsView(status="pending", reason="Fitting option quote curve", **common)
        result = saved[1]
        report = self._curve_shadow.get(ticker)
        evidence = (
            {
                "held_out_inside": result.held_out_inside,
                "held_out_count": result.held_out_count,
                "paired_held_out_count": result.paired_held_out_count,
                "paired_shadow_inside": result.paired_shadow_inside,
                "paired_benchmark_inside": result.paired_benchmark_inside,
                "one_tick_stable": None,
                "bid_ask_fit": None,
                "refresh_ms": report.get("live_refresh_ms") if report else None,
                "fit_ms": result.elapsed_ms,
                "rejection_reasons": result.rejection_reasons,
            }
        )
        common["model_evidence"] = evidence
        estimate = result.odds.get((expiry, strike))
        call_itm = _probability_tenths(estimate)
        if call_itm is None:
            reason = (
                estimate.reason if estimate is not None else
                result.contract_reasons.get((expiry, strike))
                or result.expiry_reasons.get(expiry)
                or next((name for name in ("curve_fit_failed", "invalid_spot")
                         if result.rejection_reasons.get(name)), None)
                or "curve_strike_not_supported"
            )
            evidence["one_tick_stable"] = False if reason == "one_tick_unstable" else None
            evidence["bid_ask_fit"] = False if reason == "infeasible_curve" else None
            return MarketOddsView(status="unavailable", reason=reason, **common)
        low, high, support = _quote_support(estimate, side)
        itm = call_itm if side == "call" else 1000 - call_itm
        evidence["one_tick_stable"] = True
        evidence["bid_ask_fit"] = True
        return MarketOddsView(
            status="available", itm_pct_tenths=itm, otm_pct_tenths=1000 - itm,
            bound_low_pct_tenths=low, bound_high_pct_tenths=high,
            quote_support_score=support, **common,
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
        itm = saved.call_itm_pct_tenths if side == "call" else 1000 - saved.call_itm_pct_tenths
        low, high = saved.call_bound_low_pct_tenths, saved.call_bound_high_pct_tenths
        if low is not None and high is not None and side == "put":
            low, high = 1000 - high, 1000 - low
        return MarketOddsView(
            status="available",
            itm_pct_tenths=itm,
            otm_pct_tenths=1000 - itm,
            source=saved.source,
            fetched_at=saved.fetched_at,
            session_date=saved.session_date,
            model_version=_MODEL_VERSION,
            bound_low_pct_tenths=low,
            bound_high_pct_tenths=high,
            quote_support_score=saved.quote_support_score,
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
            entry.spot_basis,
            entry.underlying_quote_time,
        )

    def pricing_selection(
        self, ticker: str, side: str, expiry: str, strike: Decimal,
        fetched_at: datetime, source: str,
    ) -> PricingSelection | None:
        """Only a currently valid snapshot of the displayed quote generation qualifies."""
        entry = self._cache.get(ticker)
        if (
            entry is None
            or not self._valid(entry, _as_utc(self.clock()))
            or entry.error is not None
            or entry.fetched_at != fetched_at
            or entry.source != source
        ):
            return None
        if (
            (expiry, strike) in entry.identity_issues
            or (expiry, strike) not in entry.valid_contracts
            or expiry in entry.reasons
            or (side, expiry, strike) in entry.quote_issues
        ):
            return None
        quote = self.entry_quote(ticker, side, expiry, strike, ticker)
        if (
            quote is None
            or quote.rate is None
            or quote.rate_as_of_session is None
            or not quote.rate.is_finite()
            or not 0 <= quote.rate <= Decimal("0.25")
        ):
            return None
        valuation_day = quote_session(quote.valuation_time)
        expiry_date = date.fromisoformat(expiry)
        if (
            not 0 <= (valuation_day - quote.rate_as_of_session).days <= 7
            or expiry_date <= valuation_day
            or expiry_session_completed(expiry_date, quote.valuation_time)
        ):
            return None
        return PricingSelection(quote)

    def underlying_quote(self, ticker: str) -> UnderlyingQuote | None:
        """Validated live stock quote, regardless of option bid/ask availability."""
        now = _as_utc(self.clock())
        entry = self._cache.get(ticker)
        if (
            entry is None
            or not regular_session_open(now)
            or not self._valid(entry, now)
            or entry.error is not None
            or entry.spot is None
            or entry.source is None
            or entry.underlying_quote_time is None
            or entry.underlying_quote_fetched_at is None
            or quote_session(entry.underlying_quote_time) != entry.session_date
        ):
            return None
        age = (now - entry.underlying_quote_time).total_seconds()
        if age < -60 or age > _REFRESH.total_seconds():
            return None
        return UnderlyingQuote(
            entry.spot,
            entry.session_date,
            entry.source,
            entry.underlying_quote_fetched_at,
            entry.underlying_quote_time,
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
        inputs = list(self._page_inputs)
        for task in inputs:
            task.cancel()
        if inputs:
            await asyncio.gather(*inputs, return_exceptions=True)
        if self._shadow_tasks:
            await asyncio.gather(*self._shadow_tasks, return_exceptions=True)

    async def _run_curve_shadow(
        self,
        ticker: str,
        snapshot: _Snapshot,
        chain_rows: list[OptionQuote],
        spot: Decimal,
        rates: dict[str, Decimal],
        allowed: set[str],
        valuation_time: datetime,
        benchmark_ms: float,
        live_refresh_ms: float,
    ) -> None:
        try:
            shadow = await asyncio.to_thread(
                calculate_curve_shadow,
                chain_rows,
                spot,
                lambda expiry: float(rates[expiry.isoformat()]),
                allowed,
                valuation_time,
                snapshot.odds,
            )
            if self._cache.get(ticker) is snapshot:
                self._record_curve_shadow(ticker, snapshot, shadow, benchmark_ms, live_refresh_ms)
        except Exception:
            LOG.exception("market curve shadow failed for %s", ticker)
            if self._cache.get(ticker) is snapshot:
                self._record_curve_shadow(
                    ticker, snapshot,
                    CurveShadowResult({}, 0, 0, 0, {"curve_fit_failed": 1}),
                    benchmark_ms, live_refresh_ms,
                )

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
        close = positive_close(history.bars, session)
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
        status = await (self._dividend_provider or fetch_dividend_status)(ticker, now)
        self._dividends[ticker] = status
        if len(self._dividends) > _MAX_CACHED_TICKERS:
            self._dividends.pop(next(iter(self._dividends)))
        return status

    def _shared_page_inputs(self, ticker: str, now: datetime) -> asyncio.Task[object]:
        """One in-flight curve read per ticker. A timed-out page leaves it running."""
        flight = self._page_input_flights.get(ticker)
        if flight is not None and not flight.done():
            return flight
        flight = asyncio.create_task(self._page_curve_and_dividends(ticker, now))
        self._page_input_flights[ticker] = flight
        self._page_inputs.add(flight)

        def _finish(done: asyncio.Task[object], symbol: str = ticker) -> None:
            self._page_inputs.discard(done)
            self._page_input_components.pop(done, None)
            if self._page_input_flights.get(symbol) is done:
                self._page_input_flights.pop(symbol, None)

        flight.add_done_callback(_finish)
        return flight

    def prefetch_page_inputs(self, ticker: str, now: datetime) -> None:
        """Start the shared Treasury and dividend flight without waiting for it."""
        self._shared_page_inputs(ticker, _as_utc(now))

    async def pricing_context_for(
        self,
        ticker: str,
        chain: OptionChainResponse,
        info: StockInfoResponse,
        history: HistoricalResponse,
        now: datetime,
    ) -> PricingContext | PricingIssue:
        """Semantic rejection never borrows a snapshot; only failed input acquisition may."""
        now = _as_utc(now)
        completed_close = (
            positive_close(history.bars, latest_completed_session(now))
            if not regular_session_open(now)
            else None
        )
        try:
            blocked = pricing_block_reason(
                chain, info, now, completed_close, quote_max_age=_REFRESH
            )
        except (ValueError, ArithmeticError):
            return PricingIssue("numerical_failure", "Pricing inputs could not be validated")
        if blocked is not None:
            return pricing_issue(blocked)
        task = self._shared_page_inputs(ticker, now)
        try:
            inputs = await asyncio.wait_for(asyncio.shield(task), PAGE_INPUT_TIMEOUT)
        except TimeoutError:
            try:
                inputs = self._page_input_values(self._page_input_components.get(task), now)
            except Exception:
                return PricingIssue("numerical_failure", "Pricing inputs could not be validated")
        except (InputAcquisitionError, httpx.HTTPError, OSError):
            inputs = _PageInputs(
                None, "failed", DividendStatus("unknown", now, acquisition_failed=True), "failed"
            )
        except Exception:
            LOG.exception("displayed-chain pricing inputs unavailable for %s", ticker)
            return PricingIssue("numerical_failure", "Pricing inputs could not be validated")
        try:
            built = build_pricing_context(
                ticker,
                chain,
                info,
                now,
                completed_close,
                inputs.curve,
                inputs.dividends,
                quote_max_age=_REFRESH,
            )
        except (ValueError, ArithmeticError, TypeError, AttributeError):
            LOG.exception("displayed chain cannot be priced for %s", ticker)
            return PricingIssue("numerical_failure", "Pricing inputs could not be validated")
        if isinstance(built, str):
            return pricing_issue(built)
        return self._apply_input_states(built, inputs)

    @staticmethod
    def _apply_input_states(built: PricingContext, inputs: _PageInputs) -> PricingContext:
        """Keep a finished semantic rejection; a still-running fetch stays pending."""
        display_rates: dict[str, Decimal] = {}
        if inputs.curve is not None and inputs.curve_state == "done":
            for expiry_text in set(built.rates) | set(built.reasons):
                rate = inputs.curve.rate_for(date.fromisoformat(expiry_text), built.valuation_time)
                if rate is not None and math.isfinite(rate) and 0 <= rate <= 0.25:
                    display_rates[expiry_text] = Decimal(str(rate))
        if inputs.curve_state == "done" and inputs.dividend_state == "done":
            return replace(built, display_rates=display_rates)
        pending: set[str] = set()
        acquisition: set[str] = set()
        reasons = dict(built.reasons)
        for expiry_text, reason in reasons.items():
            if reason == "Same-day option quote timing cannot be verified":
                continue
            if reason == "Dated Treasury rate is unavailable" and inputs.curve_state == "done":
                continue
            if inputs.dividend_state == "done" and not inputs.dividends.acquisition_failed:
                eligible, dividend_reason = inputs.dividends.eligible_for(
                    date.fromisoformat(expiry_text)
                )
                if not eligible:
                    reasons[expiry_text] = dividend_reason or "Dividend exposure is unsupported"
                    continue
            if inputs.curve_state == "failed" or inputs.dividend_state == "failed":
                acquisition.add(expiry_text)
                continue
            if inputs.curve_state == "pending" or inputs.dividend_state == "pending":
                pending.add(expiry_text)
        return replace(
            built,
            reasons=reasons,
            acquisition_expirations=acquisition,
            pending_expirations=pending,
            display_rates=display_rates,
        )

    async def _page_curve_and_dividends(
        self, ticker: str, now: datetime
    ) -> _PageInputs:
        curve = asyncio.create_task(
            fetch_treasury_curve(self.client, now, raise_on_acquisition_failure=True)
        )
        dividends = asyncio.create_task(self._dividend_status(ticker, now))
        components = (curve, dividends)
        task = asyncio.current_task()
        if task is not None:
            self._page_input_components[task] = components
        await asyncio.gather(*components, return_exceptions=True)
        return self._page_input_values(components, now)

    @staticmethod
    def _page_input_values(
        components: tuple[asyncio.Task, asyncio.Task] | None, now: datetime,
    ) -> _PageInputs:
        """Keep a finished sibling when the other input is still running."""
        curve: TreasuryCurve | None = None
        curve_state: _InputState = "failed"
        dividends = DividendStatus("unknown", now, acquisition_failed=True)
        dividend_state: _InputState = "failed"
        if components is None:
            return _PageInputs(curve, curve_state, dividends, dividend_state)
        curve_task, dividend_task = components
        if not curve_task.done():
            curve_state = "pending"
        else:
            try:
                curve = curve_task.result()
                curve_state = "done"
            except (InputAcquisitionError, TimeoutError, httpx.HTTPError, OSError):
                curve_state = "failed"
        if not dividend_task.done():
            dividends = DividendStatus("unknown", now)
            dividend_state = "pending"
        else:
            try:
                dividends = dividend_task.result()
            except (InputAcquisitionError, TimeoutError, httpx.HTTPError, OSError):
                dividend_state = "failed"
            else:
                dividend_state = "failed" if dividends.acquisition_failed else "done"
        return _PageInputs(curve, curve_state, dividends, dividend_state)

    async def _refresh(self, ticker: str) -> None:
        async with self._semaphore:
            refresh_started = time.perf_counter()
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
                chain_reason = displayed_chain_reason(chain, now)
                if chain_reason is not None:
                    self._remember(
                        ticker,
                        _Snapshot(chain.fetched_at, session, chain.source, {}, {}, chain_reason),
                    )
                    return
                # Last-sale time does not timestamp the bid and ask. After hours,
                # use only the exact official bar for the completed session.
                completed_close = (
                    await self._completed_session_close(ticker, now)
                    if not regular_session_open(now)
                    else None
                )
                blocked = pricing_block_reason(
                    chain, info, now, completed_close, quote_max_age=_REFRESH
                )
                if blocked is not None:
                    self._remember(
                        ticker,
                        _Snapshot(chain.fetched_at, session, chain.source, {}, {}, blocked),
                    )
                    return
                curve, dividends = await asyncio.gather(
                    fetch_treasury_curve(self.client, now), self._dividend_status(ticker, now)
                )
                context = build_pricing_context(
                    ticker,
                    chain,
                    info,
                    now,
                    completed_close,
                    curve,
                    dividends,
                    quote_max_age=_REFRESH,
                )
                if isinstance(context, str):
                    self._remember(
                        ticker,
                        _Snapshot(chain.fetched_at, session, chain.source, {}, {}, context),
                    )
                    return
                allowed = set(context.rates)
                odds: dict[tuple[str, Decimal], OddsEstimate] = {}
                benchmark_started = time.perf_counter()
                if allowed and curve is not None:
                    odds = await asyncio.to_thread(
                        calculate_market_odds,
                        chain.rows,
                        context.spot,
                        lambda expiry, priced=context: float(priced.rates[expiry.isoformat()]),
                        allowed,
                        context.valuation_time,
                    )
                benchmark_ms = (time.perf_counter() - benchmark_started) * 1000
                snapshot = _Snapshot(
                    context.fetched_at,
                    context.session_date,
                    context.source,
                    odds,
                    context.reasons,
                    spot=context.spot,
                    stock_ask=context.stock_ask,
                    valuation_time=context.valuation_time,
                    underlying_quote_time=context.underlying_quote_time,
                    underlying_quote_fetched_at=context.underlying_quote_fetched_at,
                    rate_as_of_session=context.rate_as_of_session,
                    rates=context.rates,
                    entry_quotes=context.entry_quotes,
                    valid_contracts=context.valid_contracts,
                    invalid_contracts=context.invalid_contracts,
                    spot_basis=context.spot_basis,
                    quote_issues=context.quote_issues,
                    identity_issues=context.identity_issues,
                )
                self._remember(ticker, snapshot)
                live_refresh_ms = (time.perf_counter() - refresh_started) * 1000
                if allowed and curve is not None and len(self._shadow_tasks) < 4:
                    # Research must not hold a live market-refresh slot.
                    task = asyncio.create_task(
                        self._run_curve_shadow(
                            ticker,
                            snapshot,
                            chain.rows,
                            context.spot,
                            context.rates,
                            allowed,
                            context.valuation_time,
                            benchmark_ms,
                            live_refresh_ms,
                        )
                    )
                    self._shadow_tasks.add(task)
                    task.add_done_callback(self._shadow_tasks.discard)
                elif allowed and curve is not None:
                    try:
                        self._record_curve_shadow(
                            ticker,
                            snapshot,
                            CurveShadowResult({}, 0, 0, 0, {"shadow_queue_full": 1}),
                            benchmark_ms,
                            live_refresh_ms,
                        )
                    except Exception:
                        LOG.exception("market curve shadow queue report failed for %s", ticker)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("market odds refresh failed for %s", ticker)
                self._remember(
                    ticker,
                    _Snapshot(
                        now,
                        quote_session(now),
                        None,
                        {},
                        {},
                        "Market data or pricing is unavailable",
                    ),
                )
