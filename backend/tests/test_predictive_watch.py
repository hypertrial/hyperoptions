"""Regression tests for the live, local-only predictive odds cache."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event

import pytest

from options_api.predictive_watch import PredictiveWatchOdds
from stocksweeper.forecast.predictive import PredictiveDistribution


SESSION = date(2026, 9, 25)
EXPIRY = date(2026, 10, 2)


def _distribution(
    session: date = SESSION, *, status: str = "available"
) -> PredictiveDistribution:
    return PredictiveDistribution(
        ticker="IREN",
        status=status,
        reason=None if status == "available" else "market_data_invalid",
        method="empirical_scaled" if status == "available" else None,
        as_of=session,
        expiry_session=EXPIRY,
        horizon_sessions=5,
        spot=100.0 if status == "available" else None,
        daily_volatility=0.02 if status == "available" else None,
        model_version="test-v1" if status == "available" else None,
        support=3 if status == "available" else 0,
        data_hash="abc123" if status == "available" else None,
        terminal_prices=(90.0, 100.0, 110.0) if status == "available" else (),
        weights=(0.2, 0.5, 0.3) if status == "available" else (),
    )


class _Calendar:
    def __init__(self) -> None:
        self.session = SESSION

    def last_completed(self, _now: datetime) -> date:
        return self.session


class _Forecaster:
    def __init__(self) -> None:
        self.calendar = _Calendar()
        self.prepared: list[date] = []
        self.success_session: date | None = None
        self.forecasts = 0
        self.fail_next = False

    def prepare(self, _ticker: str, session: date) -> None:
        self.prepared.append(session)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("source outage")
        self.success_session = session

    def forecast(
        self, _ticker: str, _now: datetime, _expiry: date, **_kwargs: object
    ) -> PredictiveDistribution:
        self.forecasts += 1
        status = "available" if self.success_session == self.calendar.session else "unavailable"
        return _distribution(self.calendar.session, status=status)


async def _settle(watch: PredictiveWatchOdds) -> None:
    while watch._tasks or watch._pending:
        await asyncio.sleep(0.01)


def test_discrete_call_put_and_atm_are_separate_and_share_one_distribution(tmp_path) -> None:
    now = datetime(2026, 9, 25, 21, tzinfo=UTC)
    forecaster = _Forecaster()
    forecaster.success_session = SESSION
    watch = PredictiveWatchOdds(tmp_path, lambda: now, forecaster=forecaster)

    call, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
    put, _ = watch.lookup("IREN", "put", EXPIRY, Decimal("100"))

    assert (call.itm_pct_tenths, call.otm_pct_tenths, call.atm_pct_tenths) == (300, 200, 500)
    assert (put.itm_pct_tenths, put.otm_pct_tenths, put.atm_pct_tenths) == (200, 300, 500)
    assert forecaster.forecasts == 1
    assert (call.method, call.as_of_session, call.expiry_session) == (
        "empirical_scaled", SESSION, EXPIRY
    )
    assert (call.model_version, call.support, call.data_hash) == ("test-v1", 3, "abc123")


@pytest.mark.asyncio
async def test_source_failure_retries_after_delay_and_session_rollover_invalidates_cache(
    tmp_path,
) -> None:
    moment = [datetime(2026, 9, 25, 21, tzinfo=UTC)]
    forecaster = _Forecaster()
    forecaster.fail_next = True
    watch = PredictiveWatchOdds(tmp_path, lambda: moment[0], forecaster=forecaster)
    try:
        watch.schedule(["IREN"])
        await _settle(watch)
        failed, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert failed.status == "unavailable"
        assert failed.reason == "market_data_invalid"
        assert failed.itm_pct_tenths is None
        assert forecaster.prepared == [SESSION]

        moment[0] += timedelta(minutes=4, seconds=59)
        watch.schedule(["IREN"])
        assert forecaster.prepared == [SESSION]

        moment[0] += timedelta(seconds=1)
        watch.schedule(["IREN"])
        await _settle(watch)
        recovered, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert recovered.status == "available"
        assert recovered.itm_pct_tenths == 300
        assert forecaster.prepared == [SESSION, SESSION]

        moment[0] = datetime(2026, 9, 28, 21, tzinfo=UTC)
        forecaster.calendar.session = date(2026, 9, 28)
        before_refresh, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert before_refresh.status == "unavailable"
        assert before_refresh.itm_pct_tenths is None
        watch.schedule(["IREN"])
        await _settle(watch)
        after_refresh, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert after_refresh.status == "available"
        assert after_refresh.as_of_session == date(2026, 9, 28)
        assert forecaster.prepared == [SESSION, SESSION, date(2026, 9, 28)]
    finally:
        await watch.close()


@pytest.mark.asyncio
async def test_local_history_is_pending_while_background_prepare_runs(tmp_path) -> None:
    started = Event()
    release = Event()
    forecaster = _Forecaster()

    def prepare(_ticker: str, session: date) -> None:
        started.set()
        release.wait(2)
        forecaster.success_session = session

    forecaster.prepare = prepare
    watch = PredictiveWatchOdds(
        tmp_path,
        lambda: datetime(2026, 9, 25, 21, tzinfo=UTC),
        forecaster=forecaster,
    )
    try:
        watch.schedule(["IREN"])
        pending, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert pending.status == "pending"
        assert pending.itm_pct_tenths is None
        assert await asyncio.to_thread(started.wait, 2)
    finally:
        release.set()
        await watch.close()
    available, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
    assert available.status == "available"
