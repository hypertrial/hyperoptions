"""Regression tests for the live, local-only predictive odds cache."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from options_api.predictive_watch import PredictiveWatchOdds
from options_api.contract_identity import make_watch_key
from options_api.market_calendar import session_close
from options_api.outcomes import TERMS_NOTE
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.calibration import build_calibration, moneyness_band
from stocksweeper.forecast.ledger import ForecastIssuance, ForecastLabel, ForecastLedger
from stocksweeper.forecast.predictive import PredictiveDistribution, SelectionEvidence


SESSION = date(2026, 9, 25)
EXPIRY = date(2026, 10, 2)


def _distribution(
    session: date = SESSION, *, status: str = "available", horizon: int = 5
) -> PredictiveDistribution:
    return PredictiveDistribution(
        ticker="IREN",
        status=status,
        reason=None if status == "available" else "market_data_invalid",
        method="empirical_scaled" if status == "available" else None,
        as_of=session,
        expiry_session=EXPIRY,
        horizon_sessions=horizon,
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
        self.horizon_sessions = 5

    def last_completed(self, _now: datetime) -> date:
        return self.session

    def horizon(self, _session: date, _expiry: date) -> int:
        return self.horizon_sessions


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
        return _distribution(
            self.calendar.session, status=status, horizon=self.calendar.horizon_sessions
        )


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
    assert (call.price_basis, call.price_as_of, call.validation_evidence) == (
        "completed_close", session_close(SESSION), None
    )


def test_verified_input_retrieval_is_cached_by_exact_forecast_identity(tmp_path) -> None:
    now = datetime(2026, 9, 25, 21, tzinfo=UTC)
    watch = PredictiveWatchOdds(tmp_path, lambda: now, forecaster=_Forecaster())
    calls: list[tuple[object, ...]] = []
    watch.ledger = SimpleNamespace(
        cache_retrieved_at=lambda *args: (calls.append(("cache", *args)), now)[1],
    )
    price_file = tmp_path / "forecast" / "prices" / "IREN.parquet"
    price_file.parent.mkdir(parents=True)
    assert watch.cache_retrieved_at("IREN", "abc123", SESSION) is None
    price_file.write_bytes(b"price")
    manifest = price_file.with_suffix(".json")
    manifest.write_text("{}")
    assert watch.cache_retrieved_at("IREN", None, SESSION) is None
    assert watch.cache_retrieved_at("IREN", "abc123", SESSION) == now
    assert watch.cache_retrieved_at("IREN", "abc123", SESSION) == now
    assert watch.cache_retrieved_at("IREN", "another-hash", SESSION) == now
    manifest.write_text('{"revised": true}')
    assert watch.cache_retrieved_at("IREN", "abc123", SESSION) is None
    manifest.unlink()
    assert watch.cache_retrieved_at("IREN", "abc123", SESSION) is None
    assert [call[0] for call in calls] == ["cache", "cache"]


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


@pytest.mark.asyncio
async def test_direct_and_scheduled_refresh_cannot_write_same_ticker_concurrently(tmp_path) -> None:
    started = Event()
    release = Event()
    guard = Lock()
    active = 0
    peak = 0
    calls = 0
    forecaster = _Forecaster()

    def prepare(_ticker: str, session: date) -> None:
        nonlocal active, peak, calls
        with guard:
            active += 1
            calls += 1
            peak = max(peak, active)
            if calls == 1:
                started.set()
        try:
            release.wait(2)
            forecaster.success_session = session
        finally:
            with guard:
                active -= 1

    forecaster.prepare = prepare
    watch = PredictiveWatchOdds(
        tmp_path,
        lambda: datetime(2026, 9, 25, 21, tzinfo=UTC),
        forecaster=forecaster,
    )
    first = asyncio.create_task(watch._refresh("IREN"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        second = asyncio.create_task(watch._refresh("IREN"))
        await asyncio.sleep(0.05)
        assert calls == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 3)
        assert calls == 2
        assert peak == 1
    finally:
        release.set()
        await watch.close()


class _CalibrationLedger:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.reads = 0

    def calibration_rows(self, *, since: date) -> list[dict[str, object]]:
        self.reads += 1
        return [row for row in self.rows if row["input_session"] >= since]


def _calibration_rows() -> list[dict[str, object]]:
    calendar = SessionCalendar()
    first = date(2025, 1, 2)
    rows = []
    for block in range(20):
        origin = calendar.offset(first, block * 2)
        expiry = calendar.offset(origin, 1)
        for index in range(25):
            ticker = chr(ord("A") + index)
            for side, probability, classification in (
                ("call", 0.6, "itm"),
                ("put", 0.4, "otm"),
            ):
                key = make_watch_key(ticker, ticker, side, expiry.isoformat(), Decimal("100"))
                rows.append(
                    {
                        "idempotency_key": f"{key}:{origin}",
                        "contract_key": key,
                        "ticker": ticker,
                        "root": ticker,
                        "side": side,
                        "expiration": expiry,
                        "expiry_session": expiry,
                        "strike_exact": "100.000",
                        "terms_note": TERMS_NOTE,
                        "input_session": origin,
                        "input_retrieved_at": session_close(origin),
                        "issued_at": session_close(origin) + timedelta(minutes=1),
                        "model_version": "test-v1",
                        "data_hash": "vintage-1",
                        "price_basis": "completed_close",
                        "spot_exact": "100.000",
                        "itm_probability": probability,
                        "snapshot_window": None,
                        "label_checked_at": session_close(expiry) + timedelta(minutes=1),
                        "label_status": "valid",
                        "label_source": "Nasdaq historical Close (Yahoo cross-check)",
                        "nasdaq_close_exact": "110.000",
                        "yahoo_close_exact": "110.004",
                        "selected_close_exact": "110.000",
                        "classification": classification,
                    }
                )
    return rows


def test_calibration_requires_500_units_and_averages_shared_call_put_close() -> None:
    rows = _calibration_rows()
    as_of = datetime(2025, 5, 1, tzinfo=UTC)
    ledger = _CalibrationLedger(rows[:-2])
    assert build_calibration(ledger, as_of) == {}
    assert ledger.reads == 1

    ledger.rows = rows
    summary = build_calibration(ledger, as_of)
    evidence = summary[("test-v1", "completed_close", "1", "near ATM")]
    assert evidence["independent_units"] == 500
    assert evidence["predicted_itm_pct_tenths"] == 500
    assert evidence["observed_itm_pct_tenths"] == 500
    assert evidence["through_session"] == rows[-1]["expiry_session"]
    assert ledger.reads == 2

    revised = dict(rows[0])
    revised["issued_at"] += timedelta(minutes=1)
    revised["idempotency_key"] += ":revision"
    revised["data_hash"] = "revised"
    revised["itm_probability"] = 0.99
    ledger.rows.append(revised)
    assert build_calibration(ledger, as_of) == summary


def test_calibration_matches_model_horizon_and_signed_moneyness(tmp_path) -> None:
    assert moneyness_band(Decimal("100"), Decimal("110"), "call") == "moderately OTM"
    assert moneyness_band(Decimal("100"), Decimal("110"), "put") == "moderately ITM"
    forecaster = _Forecaster()
    forecaster.success_session = SESSION
    forecaster.calendar.horizon_sessions = 1
    watch = PredictiveWatchOdds(
        tmp_path, lambda: datetime(2026, 9, 25, 21, tzinfo=UTC), forecaster=forecaster
    )
    watch._calibration = build_calibration(
        _CalibrationLedger(_calibration_rows()), datetime(2025, 5, 1, tzinfo=UTC)
    )
    call, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
    assert call.validation_evidence is not None
    assert call.validation_evidence.independent_units == 500
    unsupported, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("110"))
    assert unsupported.validation_evidence is None


@pytest.mark.asyncio
async def test_calibration_refresh_is_daily_and_background(tmp_path) -> None:
    now = [datetime(2025, 5, 1, tzinfo=UTC)]
    watch = PredictiveWatchOdds(tmp_path, lambda: now[0], forecaster=_Forecaster())
    ledger = _CalibrationLedger(_calibration_rows())
    watch.ledger = ledger
    watch.schedule_calibration()
    await watch._calibration_task
    assert ledger.reads == 1
    watch.schedule_calibration()
    assert ledger.reads == 1
    now[0] += timedelta(days=1)
    watch.schedule_calibration()
    await watch._calibration_task
    assert ledger.reads == 2
    await watch.close()


@pytest.mark.asyncio
async def test_mid_session_promotion_invalidates_cache_and_warms_in_background(tmp_path) -> None:
    class Promotable(_Forecaster):
        choice: str | None = None
        champion_prepared = False

        def champion_method(self, _horizon: int) -> str | None:
            return self.choice

        def prepare(self, ticker: str, session: date) -> None:
            super().prepare(ticker, session)
            if self.choice == "student_t_ewma":
                self.champion_prepared = True

        def forecast(
            self, ticker: str, now: datetime, expiry: date, **kwargs: object
        ) -> PredictiveDistribution:
            baseline = super().forecast(ticker, now, expiry, **kwargs)
            if self.choice == "student_t_ewma" and not self.champion_prepared:
                return replace(
                    baseline,
                    method="lognormal_ewma",
                    model_version="baseline-v1",
                    selection=SelectionEvidence(rejection_reason="band_champion_not_prepared"),
                )
            if self.choice == "student_t_ewma":
                return replace(baseline, method="student_t_ewma", model_version="student-v1")
            if self.choice == "lognormal_ewma":
                return replace(baseline, method="lognormal_ewma", model_version="baseline-v1")
            return baseline

    now = [datetime(2026, 9, 25, 21, tzinfo=UTC)]
    forecaster = Promotable()
    watch = PredictiveWatchOdds(tmp_path, lambda: now[0], forecaster=forecaster)
    try:
        watch.schedule(["IREN"])
        await _settle(watch)
        frozen, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert frozen.method == "empirical_scaled"
        assert forecaster.prepared == [SESSION]

        forecaster.choice = "student_t_ewma"
        forecaster.fail_next = True
        warming, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert warming.method == "lognormal_ewma"
        await _settle(watch)
        assert forecaster.prepared == [SESSION, SESSION]
        still_warming, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert still_warming.method == "lognormal_ewma"
        assert not watch._tasks

        now[0] += timedelta(minutes=5)
        # The cached fallback retries after the window without waiting for a
        # new session or another source revision.
        cached_fallback, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert cached_fallback.method == "lognormal_ewma"
        await _settle(watch)
        promoted, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert promoted.method == "student_t_ewma"
        assert forecaster.prepared == [SESSION, SESSION, SESSION]

        forecaster.choice = "lognormal_ewma"
        rolled_back, _ = watch.lookup("IREN", "call", EXPIRY, Decimal("100"))
        assert rolled_back.method == "lognormal_ewma"
        assert forecaster.forecasts == 5
    finally:
        await watch.close()


def test_calibration_query_uses_latest_exact_label_without_scenarios(tmp_path) -> None:
    ledger = ForecastLedger(tmp_path)
    key = make_watch_key("IREN", "IREN", "call", EXPIRY.isoformat(), Decimal("100"))
    distribution = _distribution()
    ledger.record_batch(
        [
            (
                ForecastIssuance(
                    contract_key=key,
                    ticker="IREN",
                    root="IREN",
                    side="call",
                    expiration=EXPIRY,
                    expiry_session=EXPIRY,
                    strike_exact="100.000",
                    terms_note=TERMS_NOTE,
                    contract_since=SESSION,
                    input_session=SESSION,
                    input_retrieved_at=session_close(SESSION),
                    issued_at=session_close(SESSION) + timedelta(minutes=1),
                    model_version="test-v1",
                    method="empirical_scaled",
                    data_hash="abc123",
                    distribution_hash=None,
                    price_basis="completed_close",
                    spot_exact="100.000",
                    status="available",
                    itm_probability=0.3,
                    otm_probability=0.2,
                    atm_probability=0.5,
                    unavailable_reason=None,
                ),
                distribution,
            )
        ]
    )
    checked_at = session_close(EXPIRY) + timedelta(minutes=1)
    valid = ForecastLabel(
        contract_key=key,
        terms_note=TERMS_NOTE,
        expiry_session=EXPIRY,
        checked_at=checked_at,
        status="valid",
        reason=None,
        source="Nasdaq historical Close (Yahoo cross-check)",
        nasdaq_close_exact="110.000",
        yahoo_close_exact="110.004",
        selected_close_exact="110.000",
        classification="itm",
    )
    ledger.record_label(valid)
    rows = ledger.calibration_rows(since=SESSION)
    assert len(rows) == 1
    assert rows[0]["nasdaq_close_exact"] == "110.000"
    assert "terminal_prices" not in rows[0]
    ledger.record_label(
        replace(
            valid,
            checked_at=checked_at + timedelta(days=1),
            status="excluded",
            reason="source_conflict",
            source=None,
            nasdaq_close_exact=None,
            yahoo_close_exact=None,
            selected_close_exact=None,
            classification=None,
        )
    )
    assert ledger.calibration_rows(since=SESSION) == []
