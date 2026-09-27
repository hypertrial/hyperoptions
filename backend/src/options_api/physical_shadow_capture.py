"""Bounded, as-issued capture and cached results for model comparison."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

from options_api.market_calendar import session_close
from options_api.intraday_shadow import _VERSION as INTRADAY_VERSION, forecast_intraday_shadow
from options_api.market_watch import MarketWatchOdds, UnderlyingQuote
from options_api.predictive_watch import PredictiveWatchOdds
from stocksweeper.forecast.ledger import ForecastIssuance
from stocksweeper.forecast.physical_contest import (
    EMPIRICAL_SHADOW_VERSION, GJR_VERSION, STUDENT_VERSION,
    PhysicalShadowForecaster, ShadowForecast,
)
from stocksweeper.forecast.predictive import BASELINE_VERSION, PredictiveDistribution

LOG = logging.getLogger(__name__)
_MAX_BATCH_CONTRACTS = 512
_MAX_PENDING_BATCHES = 4
_MAX_CACHED_GROUPS = 512
_RETRY_SECONDS = 30
MODEL_VERSIONS = {
    "lognormal_ewma": BASELINE_VERSION,
    "empirical_scaled": EMPIRICAL_SHADOW_VERSION,
    "student_t_ewma": STUDENT_VERSION,
    "gjr_garch_t": GJR_VERSION,
    "intraday_shadow": INTRADAY_VERSION,
}

Entry = tuple[ForecastIssuance, PredictiveDistribution | None]


class PhysicalShadowCapture:
    def __init__(
        self, predictive: PredictiveWatchOdds, market: MarketWatchOdds | None = None
    ) -> None:
        self.predictive = predictive
        self.market = market
        self.forecaster = PhysicalShadowForecaster(predictive.forecaster)
        self._semaphore = asyncio.Semaphore(1)
        self._tasks: set[asyncio.Task[None]] = set()
        self._fit_tasks: set[asyncio.Task[None]] = set()
        self._recent: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._live: OrderedDict[
            tuple[str, object, object, object, str],
            tuple[dict[str, ShadowForecast], UnderlyingQuote | None],
        ] = OrderedDict()
        self._closed = False

    def candidate(
        self,
        base: PredictiveDistribution,
        method: str,
        expiry: object,
        contract_since: object,
        quote: UnderlyingQuote | None = None,
    ) -> ShadowForecast:
        if method == "intraday_shadow" and quote is None:
            return ShadowForecast(None, "underlying_quote_unavailable", 0, 0)
        if base.status != "available" or base.data_hash is None:
            return ShadowForecast(None, base.reason or "completed_close_forecast_unavailable", 0, 0)
        if base.horizon_sessions > 25 and method != "lognormal_ewma":
            return ShadowForecast(None, "candidate_horizon_unsupported", 0, 0)
        if method == base.method and method != "intraday_shadow":
            return ShadowForecast(base, None, 0, 0)
        key = base.ticker, base.as_of, expiry, contract_since, base.data_hash
        cached = self._live.get(key)
        if cached is not None:
            results, saved_quote = cached
            if method != "intraday_shadow" or saved_quote == quote:
                return results.get(method, ShadowForecast(None, "candidate_not_prepared", 0, 0))
        return ShadowForecast(None, "candidate_not_prepared", 0, 0)

    def submit(self, entries: list[Entry]) -> None:
        if self._closed or not entries:
            return
        quotes = (
            {issue.ticker: self.market.underlying_quote(issue.ticker) for issue, _ in entries}
            if self.market is not None
            else {}
        )
        fingerprint = hashlib.sha256(
            "|".join(
                sorted(
                    f"{issue.contract_key}:{issue.input_session}:{issue.data_hash}:"
                    f"{issue.terms_note}:{issue.contract_since}:"
                    f"{quotes.get(issue.ticker)!r}"
                    for issue, _ in entries
                )
            ).encode()
        ).hexdigest()
        now = monotonic()
        previous = self._recent.get(fingerprint)
        if previous is not None:
            attempted_at, state = previous
            if state == "running" or (state == "retry" and now - attempted_at < _RETRY_SECONDS):
                return
            if state == "complete" and all(
                not issue.data_hash or (
                    issue.ticker, issue.input_session, issue.expiration,
                    issue.contract_since, issue.data_hash,
                ) in self._live
                for issue, _ in entries
            ):
                return
        self._recent[fingerprint] = (now, "running")
        self._recent.move_to_end(fingerprint)
        while len(self._recent) > 128:
            self._recent.popitem(last=False)
        if len(self._fit_tasks) >= _MAX_PENDING_BATCHES:
            # Backpressure is cheaper than an unbounded research queue.
            self._recent[fingerprint] = (now, "retry")
            task = asyncio.create_task(self._record_capacity_async(entries, quotes))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        task = asyncio.create_task(self._capture(entries, quotes))
        self._fit_tasks.add(task)
        task.add_done_callback(self._fit_tasks.discard)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda done, key=fingerprint: self._capture_done(key, done))

    def _capture_done(self, fingerprint: str, task: asyncio.Task[bool]) -> None:
        try:
            complete = not task.cancelled() and task.result() is True
        except Exception:
            complete = False
        self._recent[fingerprint] = (monotonic(), "complete" if complete else "retry")
        self._recent.move_to_end(fingerprint)

    @staticmethod
    def _capacity_entries(entries: list[Entry]) -> list[Entry]:
        return [
            (
                replace(
                    issue,
                    issued_at=datetime.now(UTC),
                    model_version=MODEL_VERSIONS[method],
                    method=method,
                    distribution_hash=None,
                    status="unavailable",
                    itm_probability=None,
                    otm_probability=None,
                    atm_probability=None,
                    unavailable_reason="shadow_capacity_exceeded",
                    known_event_status="unknown",
                ),
                None,
            )
            for issue, _ in entries
            for method in ("lognormal_ewma", "empirical_scaled", "student_t_ewma", "gjr_garch_t")
        ]

    def _record_capacity(self, entries: list[Entry]) -> None:
        self.predictive.ledger.record_batch(self._capacity_entries(entries))

    def _cache_capacity(
        self,
        entries: list[Entry],
        quotes: dict[str, UnderlyingQuote | None],
        target: dict | None = None,
    ) -> None:
        target = self._live if target is None else target
        for issue, distribution in sorted(entries, key=lambda pair: pair[1] is None):
            if not issue.data_hash:
                continue
            key = (
                issue.ticker, issue.input_session, issue.expiration,
                issue.contract_since, issue.data_hash,
            )
            if key in target or key in self._live:
                continue
            rejected = {
                method: ShadowForecast(None, "shadow_capacity_exceeded", 0, 0)
                for method in (
                    "lognormal_ewma", "empirical_scaled", "student_t_ewma",
                    "gjr_garch_t", "intraday_shadow",
                )
            }
            if distribution is not None and distribution.method == "lognormal_ewma":
                rejected["lognormal_ewma"] = ShadowForecast(distribution, None, 0, 0)
            target[key] = rejected, quotes.get(issue.ticker)

    async def _record_capacity_async(
        self, entries: list[Entry], quotes: dict[str, UnderlyingQuote | None]
    ) -> None:
        try:
            await asyncio.to_thread(self._record_capacity, entries)
        except Exception:
            LOG.exception("physical shadow capacity recording failed")
        else:
            self._cache_capacity(entries, quotes)
            while len(self._live) > _MAX_CACHED_GROUPS:
                self._live.popitem(last=False)

    async def _capture(
        self, entries: list[Entry], quotes: dict[str, UnderlyingQuote | None]
    ) -> bool:
        async with self._semaphore:
            try:
                return await asyncio.to_thread(self._capture_sync, entries, quotes)
            except Exception:
                LOG.exception("physical forecast shadow capture failed")
                return False

    def _capture_sync(
        self, entries: list[Entry], quotes: dict[str, UnderlyingQuote | None] | None = None
    ) -> bool:
        quotes = quotes or {}
        selected = sorted(
            entries, key=lambda pair: hashlib.sha256(pair[0].contract_key.encode()).digest()
        )[:_MAX_BATCH_CONTRACTS]
        selected_keys = {issue.contract_key for issue, _ in selected}
        groups: dict[tuple[str, object, object, object], list[ForecastIssuance]] = {}
        live_updates: dict[
            tuple[str, object, object, object, str],
            tuple[dict[str, ShadowForecast], UnderlyingQuote | None],
        ] = {}
        for issue, _ in selected:
            key = (issue.ticker, issue.input_session, issue.expiration, issue.contract_since)
            groups.setdefault(key, []).append(issue)
        recorded: list[Entry] = []
        for (ticker, input_session, expiry, contract_since), contracts in groups.items():
            reference = next(
                (issue for issue in contracts if issue.status == "available"), contracts[0]
            )
            candidates = None
            fit_failed = False
            if (
                reference.status == "available"
                and input_session is not None
                and reference.data_hash
                and reference.input_retrieved_at is not None
                and self.predictive.cache_retrieved_at(ticker, reference.data_hash, input_session)
                is not None
            ):
                try:
                    candidates = self.forecaster.forecast_candidates(
                        ticker,
                        session_close(input_session) + timedelta(seconds=1),
                        expiry,
                        contract_since=contract_since,
                    )
                except Exception:
                    LOG.exception("physical shadow fit failed for %s", ticker)
                    fit_failed = True
            if (
                candidates is not None
                and (baseline := candidates["lognormal_ewma"].distribution) is not None
                and baseline.data_hash == reference.data_hash
                and baseline.as_of == reference.input_session
            ):
                quote = quotes.get(ticker)
                if quote is not None:
                    try:
                        intraday = forecast_intraday_shadow(
                            self.predictive.forecaster, baseline, quote
                        )
                        candidates["intraday_shadow"] = ShadowForecast(
                            intraday.distribution, intraday.reason, 0, 0
                        )
                    except (OSError, ValueError, OverflowError):
                        candidates["intraday_shadow"] = ShadowForecast(
                            None, "intraday_model_failed", 0, 0
                        )
                live_updates[
                    (ticker, input_session, expiry, contract_since, reference.data_hash)
                ] = candidates, quote
            group_live: dict[str, ShadowForecast] = {}
            for issue in contracts:
                baseline = candidates.get("lognormal_ewma") if candidates is not None else None
                volatility = (
                    baseline.distribution.daily_volatility
                    if baseline is not None and baseline.distribution is not None
                    else None
                )
                regime = (
                    "low" if volatility < 0.02 else "medium" if volatility < 0.05 else "high"
                ) if volatility is not None else "unknown"
                for method in (
                    "lognormal_ewma",
                    "empirical_scaled",
                    "student_t_ewma",
                    "gjr_garch_t",
                ):
                    candidate = candidates.get(method) if candidates is not None else None
                    distribution = candidate.distribution if candidate is not None else None
                    reason = (
                        "shadow_fit_failed"
                        if fit_failed
                        else "input_provenance_unverified"
                        if reference.status == "available" and candidates is None
                        else issue.unavailable_reason or "primary_forecast_unavailable"
                    )
                    if candidate is not None:
                        reason = candidate.reason or "candidate_unavailable"
                    if distribution is not None and (
                        issue.status != "available"
                        or distribution.data_hash != issue.data_hash
                        or distribution.as_of != issue.input_session
                        or distribution.expiry_session != issue.expiry_session
                    ):
                        distribution = None
                        reason = "input_vintage_changed"
                    strike = Decimal(issue.strike_exact)
                    call = distribution.probability("call", strike) if distribution else None
                    put = distribution.probability("put", strike) if distribution else None
                    if distribution is not None and (
                        call is None or put is None or call + put > 1 + 1e-9
                    ):
                        distribution = None
                        reason = "candidate_probability_invalid"
                    available = distribution is not None
                    itm = (call if issue.side == "call" else put) if available else None
                    otm = (put if issue.side == "call" else call) if available else None
                    recorded.append(
                        (
                            replace(
                                issue,
                                issued_at=datetime.now(UTC),
                                model_version=(
                                    distribution.model_version
                                    if available
                                    else MODEL_VERSIONS[method]
                                ),
                                method=method,
                                distribution_hash=None,
                                price_basis="completed_close" if available else issue.price_basis,
                                spot_exact=str(distribution.spot)
                                if available
                                else issue.spot_exact,
                                status="available" if available else "unavailable",
                                itm_probability=itm,
                                otm_probability=otm,
                                atm_probability=max(0.0, 1.0 - call - put) if available else None,
                                unavailable_reason=None if available else reason,
                                volatility_regime=regime,
                                known_event_status="unknown",
                                prepare_ms=candidate.prepare_ms if candidate is not None else None,
                                lookup_ms=candidate.lookup_ms if candidate is not None else None,
                            ),
                            distribution,
                        )
                    )
                    if issue is reference:
                        group_live[method] = ShadowForecast(
                            distribution,
                            None if available else reason,
                            candidate.prepare_ms if candidate is not None else 0,
                            candidate.lookup_ms if candidate is not None else 0,
                            candidate.independent_blocks if candidate is not None else None,
                        )
            if reference.data_hash and (
                ticker, input_session, expiry, contract_since, reference.data_hash
            ) not in live_updates:
                group_live["intraday_shadow"] = ShadowForecast(
                    None,
                    group_live["lognormal_ewma"].reason,
                    0,
                    0,
                )
                live_updates[
                    (ticker, input_session, expiry, contract_since, reference.data_hash)
                ] = group_live, quotes.get(ticker)
        unselected = [entry for entry in entries if entry[0].contract_key not in selected_keys]
        recorded.extend(self._capacity_entries(unselected))
        self.predictive.ledger.record_batch(recorded)
        self._cache_capacity(unselected, quotes, live_updates)
        self._live.update(live_updates)
        while len(self._live) > _MAX_CACHED_GROUPS:
            self._live.popitem(last=False)
        retryable = {
            "shadow_fit_failed", "input_provenance_unverified", "shadow_capacity_exceeded"
        }
        return not any(
            forecast.reason in retryable
            for forecasts, _ in live_updates.values()
            for forecast in forecasts.values()
        )

    async def close(self) -> None:
        self._closed = True
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
