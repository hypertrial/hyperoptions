"""Bounded background price refresh and local-only expiry forecasts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from options_api.models import PredictiveOddsView, Side
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
        self.clock = clock
        self.forecaster = forecaster or PredictiveForecaster(data_dir)
        self.refresh_enabled = refresh_enabled
        self._attempts: dict[str, tuple[date, datetime, bool]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, None] = {}
        self._cache: dict[tuple[str, date, date, date | None, bool], PredictiveDistribution] = {}
        self._semaphore = asyncio.Semaphore(2)
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

    async def _refresh(self, ticker: str) -> None:
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
        result = self.forecaster.forecast(
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
        common = {
            "method": distribution.method,
            "as_of_session": distribution.as_of,
            "expiry_session": distribution.expiry_session,
            "model_version": distribution.model_version,
            "support": distribution.support or None,
            "data_hash": distribution.data_hash,
        }
        if distribution.status != "available":
            refreshing = ticker in self._tasks or ticker in self._pending
            return PredictiveOddsView(
                status="pending" if refreshing else "unavailable",
                reason=(
                    "Refreshing completed price history" if refreshing else distribution.reason
                ),
                **common,
            ), distribution
        call = distribution.probability("call", strike)
        put = distribution.probability("put", strike)
        if call is None or put is None:
            return PredictiveOddsView(
                status="unavailable", reason="model_probability_invalid", **common
            ), distribution
        if not 0 <= call <= 1 or not 0 <= put <= 1 or call + put > 1 + 1e-9:
            return PredictiveOddsView(
                status="unavailable", reason="model_probability_invalid", **common
            ), distribution
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
        return PredictiveOddsView(
            status="available",
            itm_pct_tenths=itm,
            otm_pct_tenths=otm,
            atm_pct_tenths=1000 - itm - otm,
            **common,
        ), distribution

    async def close(self) -> None:
        self._closed = True
        self._pending.clear()
        tasks = list(self._tasks.values())
        # asyncio.to_thread cannot stop a running provider call. Let active
        # refreshes finish before the app releases its single-instance data
        # directory lock; otherwise a new process could write the same cache.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
