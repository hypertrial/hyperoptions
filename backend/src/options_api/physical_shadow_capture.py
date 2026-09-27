"""Bounded, as-issued capture of research forecasts; never supplies live odds."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from options_api.market_calendar import session_close
from options_api.predictive_watch import PredictiveWatchOdds
from stocksweeper.forecast.ledger import ForecastIssuance
from stocksweeper.forecast.physical_contest import PhysicalShadowForecaster
from stocksweeper.forecast.predictive import PredictiveDistribution

LOG = logging.getLogger(__name__)
_MAX_BATCH_CONTRACTS = 512
_MAX_PENDING_BATCHES = 4

Entry = tuple[ForecastIssuance, PredictiveDistribution | None]


class PhysicalShadowCapture:
    def __init__(self, predictive: PredictiveWatchOdds) -> None:
        self.predictive = predictive
        self.forecaster = PhysicalShadowForecaster(predictive.forecaster)
        self._semaphore = asyncio.Semaphore(1)
        self._tasks: set[asyncio.Task[None]] = set()
        self._fit_tasks: set[asyncio.Task[None]] = set()
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._closed = False

    def submit(self, entries: list[Entry]) -> None:
        if self._closed or not entries:
            return
        fingerprint = hashlib.sha256(
            "|".join(
                sorted(
                    f"{issue.contract_key}:{issue.input_session}:{issue.data_hash}:"
                    f"{issue.terms_note}:{issue.contract_since}"
                    for issue, _ in entries
                )
            ).encode()
        ).hexdigest()
        if fingerprint in self._recent:
            return
        self._recent[fingerprint] = None
        while len(self._recent) > 128:
            self._recent.popitem(last=False)
        if len(self._fit_tasks) >= _MAX_PENDING_BATCHES:
            # Backpressure is cheaper than an unbounded research queue.
            task = asyncio.create_task(self._record_capacity_async(entries))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        task = asyncio.create_task(self._capture(entries))
        self._fit_tasks.add(task)
        task.add_done_callback(self._fit_tasks.discard)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _capacity_entries(entries: list[Entry]) -> list[Entry]:
        return [
            (
                replace(
                    issue,
                    issued_at=datetime.now(UTC),
                    model_version=method + "-shadow-v1",
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

    async def _record_capacity_async(self, entries: list[Entry]) -> None:
        try:
            await asyncio.to_thread(self._record_capacity, entries)
        except Exception:
            LOG.exception("physical shadow capacity recording failed")

    async def _capture(self, entries: list[Entry]) -> None:
        async with self._semaphore:
            try:
                await asyncio.to_thread(self._capture_sync, entries)
            except Exception:
                LOG.exception("physical forecast shadow capture failed")

    def _capture_sync(self, entries: list[Entry]) -> None:
        selected = sorted(
            entries, key=lambda pair: hashlib.sha256(pair[0].contract_key.encode()).digest()
        )[:_MAX_BATCH_CONTRACTS]
        selected_keys = {issue.contract_key for issue, _ in selected}
        groups: dict[tuple[str, object, object, object], list[ForecastIssuance]] = {}
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
                                    else method + "-shadow-v1"
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
        recorded.extend(
            self._capacity_entries(
                [entry for entry in entries if entry[0].contract_key not in selected_keys]
            )
        )
        self.predictive.ledger.record_batch(recorded)

    async def close(self) -> None:
        self._closed = True
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
