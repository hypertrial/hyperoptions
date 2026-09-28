"""Bounded background price refresh and local-only expiry forecasts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import ceil, sqrt
from pathlib import Path

from options_api.market_calendar import session_close
from options_api.models import (
    PredictiveOddsView,
    PredictiveValidationEvidence,
    Side,
    normalize_ticker,
)
from stocksweeper.forecast.calibration import build_calibration, horizon_band, moneyness_band
from stocksweeper.forecast.evidence_reports import build_model_evidence
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.forecast.predictive import PredictiveDistribution, PredictiveForecaster

LOG = logging.getLogger(__name__)
_RETRY = timedelta(minutes=5)
_MAX_PENDING = 320
_MAX_SCHEDULED = 8
_MAX_CACHED_DISTRIBUTIONS = 512


class PredictiveWatchOdds:
    def __init__(
        self,
        data_dir: Path,
        clock: Callable[[], datetime],
        forecaster: PredictiveForecaster | None = None,
        *,
        refresh_enabled: bool = True,
    ) -> None:
        self.data_dir = data_dir
        self.clock = clock
        self.forecaster = forecaster or PredictiveForecaster(data_dir)
        self.ledger = ForecastLedger(data_dir)
        self.refresh_enabled = refresh_enabled
        self._attempts: dict[str, tuple[date, datetime, bool]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, None] = {}
        self._cache: dict[
            tuple[str, date, date, date | None, bool], PredictiveDistribution
        ] = {}
        self._retrieved_at: dict[
            tuple[str, date, str], tuple[datetime, tuple[int, ...]]
        ] = {}
        self._semaphore = asyncio.Semaphore(2)
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._calibration: dict[tuple[str, str, str, str, Side], dict[str, object]] = {}
        self._calibration_day: date | None = None
        self._model_evidence: dict[tuple[str, str], dict[str, object]] = {}
        self._model_evidence_day: date | None = None
        self._calibration_task: asyncio.Task[None] | None = None
        self._closed = False

    def _now(self) -> datetime:
        value = self.clock()
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def schedule(self, tickers: Iterable[str]) -> None:
        if self._closed or not self.refresh_enabled:
            return
        now = self._now()
        completed = self.forecaster.calendar.last_completed(now)
        for ticker in sorted(set(tickers)):
            if ticker in self._tasks or ticker in self._pending:
                continue
            prior = self._attempts.get(ticker)
            if (
                prior is not None
                and prior[0] == completed
                and (prior[2] or now - prior[1] < _RETRY)
            ):
                continue
            if len(self._pending) >= _MAX_PENDING:
                break
            self._pending[ticker] = None
        self._pump()

    def _pump(self) -> None:
        while not self._closed and self._pending and len(self._tasks) < _MAX_SCHEDULED:
            ticker = next(iter(self._pending))
            self._pending.pop(ticker)
            task = asyncio.create_task(self._refresh(ticker))
            self._tasks[ticker] = task
            task.add_done_callback(lambda _task, symbol=ticker: self._done(symbol))

    def _done(self, ticker: str) -> None:
        self._tasks.pop(ticker, None)
        self._pump()

    def schedule_calibration(self) -> None:
        """Refresh displayed reliability once per UTC day without blocking pages."""
        if (
            self._closed
            or (
                self._now().date() == self._calibration_day
                and self._now().date() == self._model_evidence_day
            )
            or (self._calibration_task is not None and not self._calibration_task.done())
        ):
            return
        self._calibration_task = asyncio.create_task(self._refresh_calibration())

    async def _refresh_calibration(self) -> None:
        now = self._now()
        if self._calibration_day != now.date():
            try:
                summary = await asyncio.to_thread(build_calibration, self.ledger, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.warning("forecast calibration refresh failed", exc_info=True)
            else:
                self._calibration = summary
                self._calibration_day = now.date()
        if self._model_evidence_day != now.date():
            try:
                reports = await asyncio.to_thread(
                    build_model_evidence, self.data_dir, self.ledger, now
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.warning("model evidence refresh failed", exc_info=True)
            else:
                self._model_evidence = reports
                self._model_evidence_day = now.date()

    def evidence_for(self, method: str, horizon_sessions: int) -> dict[str, object] | None:
        band = horizon_band(horizon_sessions)
        return self._model_evidence.get((method, band)) if band else None

    def evidence_index(self) -> dict[str, object]:
        return {
            f"{method}:{band}": report
            for (method, band), report in self._model_evidence.items()
        }

    async def _refresh(self, ticker: str) -> None:
        lock = self._refresh_locks.setdefault(ticker, asyncio.Lock())
        async with lock:
            await self._refresh_locked(ticker)

    async def _refresh_locked(self, ticker: str) -> None:
        async with self._semaphore:
            now = self._now()
            completed = self.forecaster.calendar.last_completed(now)
            success = False
            try:
                await asyncio.to_thread(self.forecaster.prepare, ticker, completed)
                success = True
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.warning("predictive price refresh failed for %s", ticker, exc_info=True)
            finally:
                self._attempts[ticker] = (completed, now, success)
                for key in tuple(self._cache):
                    if key[0] == ticker:
                        self._cache.pop(key)
                for key in tuple(self._retrieved_at):
                    if key[0] == ticker:
                        self._retrieved_at.pop(key)

    def cache_retrieved_at(
        self, ticker: str, data_hash: str | None, input_session: date
    ) -> datetime | None:
        """Return the source retrieval time only for the matching verified input."""
        if not data_hash or normalize_ticker(ticker) != ticker:
            return None
        price_file = self.data_dir / "forecast" / "prices" / f"{ticker}.parquet"
        manifest = price_file.with_suffix(".json")

        def fingerprint() -> tuple[int, ...] | None:
            try:
                price = price_file.stat()
                meta = manifest.stat()
            except OSError:
                return None
            return (
                price.st_ino, price.st_mtime_ns, price.st_size,
                meta.st_ino, meta.st_mtime_ns, meta.st_size,
            )

        before = fingerprint()
        if before is None:
            return None
        key = (ticker, input_session, data_hash)
        cached = self._retrieved_at.get(key)
        if cached is not None:
            return cached[0] if cached[1] == before else None
        try:
            retrieved = self.ledger.cache_retrieved_at(ticker, data_hash, input_session)
        except (OSError, ValueError):
            LOG.warning("predictive input provenance unavailable for %s", ticker, exc_info=True)
            return None
        if retrieved is not None and fingerprint() == before:
            self._retrieved_at[key] = (retrieved, before)
            if len(self._retrieved_at) > 128:
                self._retrieved_at.pop(next(iter(self._retrieved_at)))
            return retrieved
        return None

    def distribution(
        self,
        ticker: str,
        expiry: date,
        *,
        contract_since: date | None = None,
        standard_terms: bool = True,
    ) -> PredictiveDistribution:
        now = self._now()
        completed = self.forecaster.calendar.last_completed(now)
        key = (ticker, expiry, completed, contract_since, standard_terms)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = getattr(self.forecaster, "forecast_baseline", self.forecaster.forecast)(
            ticker,
            now,
            expiry,
            contract_since=contract_since,
            standard_terms=standard_terms,
        )
        self._cache[key] = result
        if len(self._cache) > _MAX_CACHED_DISTRIBUTIONS:
            self._cache.pop(next(iter(self._cache)))
        return result

    def lookup(
        self,
        ticker: str,
        side: Side,
        expiry: date,
        strike: Decimal,
        *,
        contract_since: date | None = None,
        standard_terms: bool = True,
    ) -> tuple[PredictiveOddsView, PredictiveDistribution]:
        distribution = self.distribution(
            ticker,
            expiry,
            contract_since=contract_since,
            standard_terms=standard_terms,
        )
        return self.view_for_distribution(
            distribution, side, strike, standard_terms=standard_terms, ticker=ticker
        ), distribution

    def view_for_distribution(
        self,
        distribution: PredictiveDistribution,
        side: Side,
        strike: Decimal,
        *,
        standard_terms: bool = True,
        ticker: str | None = None,
        price_basis: str = "completed_close",
        price_as_of: datetime | None = None,
    ) -> PredictiveOddsView:
        ticker = ticker or distribution.ticker
        common = {
            "method": distribution.method,
            "as_of_session": distribution.as_of,
            "expiry_session": distribution.expiry_session,
            "model_version": distribution.model_version,
            "support": distribution.support or None,
            "data_hash": distribution.data_hash,
            "price_basis": price_basis if distribution.status == "available" else None,
            "price_as_of": (
                price_as_of or session_close(distribution.as_of)
                if distribution.status == "available" else None
            ),
            "evidence_key": (
                f"{distribution.method}:{band}"
                if distribution.method and (band := horizon_band(distribution.horizon_sessions))
                else None
            ),
        }
        if distribution.status != "available":
            refreshing = standard_terms and (ticker in self._tasks or ticker in self._pending)
            return PredictiveOddsView(
                status="pending" if refreshing else "unavailable",
                reason=(
                    "Refreshing completed price history" if refreshing else distribution.reason
                ),
                **common,
            )
        call = distribution.probability("call", strike)
        put = distribution.probability("put", strike)
        if call is None or put is None:
            return PredictiveOddsView(
                status="unavailable", reason="model_probability_invalid", **common
            )
        if not 0 <= call <= 1 or not 0 <= put <= 1 or call + put > 1 + 1e-9:
            return PredictiveOddsView(
                status="unavailable", reason="model_probability_invalid", **common
            )
        call_tenths = int(
            (Decimal(str(call)) * 1000).to_integral_value(rounding=ROUND_HALF_UP)
        )
        put_tenths = int(
            (Decimal(str(put)) * 1000).to_integral_value(rounding=ROUND_HALF_UP)
        )
        # ATM includes discrete empirical mass exactly at the strike. Keep the
        # displayed partition additive after integer rounding.
        if call_tenths + put_tenths > 1000:
            put_tenths = 1000 - call_tenths
        itm = call_tenths if side == "call" else put_tenths
        otm = put_tenths if side == "call" else call_tenths
        band = horizon_band(distribution.horizon_sessions)
        money = (
            moneyness_band(Decimal(str(distribution.spot)), strike, side)
            if distribution.spot is not None
            else None
        )
        evidence = (
            self._calibration.get(
                (distribution.model_version, price_basis, band, money, side)
            )
            if distribution.model_version and band and money
            else None
        )
        return PredictiveOddsView(
            status="available",
            itm_pct_tenths=itm,
            otm_pct_tenths=otm,
            atm_pct_tenths=1000 - itm - otm,
            simulation_error_95_pct_tenths=(
                ceil(980 / sqrt(distribution.support))
                if distribution.method not in {
                    "lognormal_ewma", "empirical_scaled", "intraday_shadow"
                }
                and distribution.support > 0
                else None
            ),
            validation_evidence=(
                PredictiveValidationEvidence.model_validate(evidence) if evidence else None
            ),
            **common,
        )

    async def close(self) -> None:
        self._closed = True
        self._pending.clear()
        tasks = list(self._tasks.values())
        # asyncio.to_thread cannot stop a running provider call. Let active
        # refreshes finish before the app releases its single-instance data
        # directory lock; otherwise a new process could write the same cache.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._calibration_task is not None:
            await asyncio.gather(self._calibration_task, return_exceptions=True)
