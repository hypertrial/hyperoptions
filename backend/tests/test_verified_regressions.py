"""Point-in-time evidence and ownership at completed-job boundaries."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import threading

import pytest

from stocksweeper.forecast.audit import AuditCohort, AuditMember
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.calibration import build_calibration
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.forecast.physical_contest import PhysicalShadowForecaster, ShadowForecast
from stocksweeper.forecast.predictive import PredictiveDistribution
from stocksweeper.pipeline.jobs import JobManager
from stocksweeper.storage.db import connect

from .test_forecast_ledger import EXPIRY, T0, issue, label
from .test_physical_contest import _bars
from .test_predictive_watch import _calibration_rows


@pytest.mark.parametrize("status", ["valid", "excluded", "pending"])
def test_future_label_revision_does_not_change_historical_calibration(tmp_path, status):
    ledger = ForecastLedger(tmp_path)
    fixture = _calibration_rows()
    issue_columns = [
        "idempotency_key", "contract_key", "ticker", "root", "side", "expiration",
        "expiry_session", "strike_exact", "terms_note", "input_session", "input_retrieved_at",
        "issued_at", "model_version", "data_hash", "price_basis", "spot_exact",
        "itm_probability", "snapshot_window",
    ]
    label_columns = [
        "contract_key", "terms_note", "expiry_session", "nasdaq_close_exact", "yahoo_close_exact",
        "selected_close_exact", "classification",
    ]
    with connect(ledger.path) as connection:
        connection.executemany(
            f"""INSERT INTO forecast_issuances ({','.join(issue_columns)},method,status,provenance)
                VALUES ({','.join('?' for _ in issue_columns)},
                        'lognormal_ewma','available','as_issued')""",
            [[row[column] for column in issue_columns] for row in fixture],
        )
        connection.executemany(
            f"""INSERT INTO forecast_labels ({','.join(label_columns)},
                   idempotency_key,checked_at,status,source)
                VALUES ({','.join('?' for _ in label_columns)},?,?,'valid',?)""",
            [
                [row[column] for column in label_columns]
                + ["label-" + row["idempotency_key"], row["label_checked_at"], row["label_source"]]
                for row in fixture
            ],
        )
    cutoff = datetime(2025, 5, 1, tzinfo=UTC)
    before = build_calibration(ledger, cutoff)
    assert len(before) == 2
    assert {entry["independent_units"] for entry in before.values()} == {500}
    with connect(ledger.path) as connection:
        connection.execute(
            """INSERT INTO forecast_labels (
                   idempotency_key,contract_key,terms_note,expiry_session,checked_at,status,
                   source,nasdaq_close_exact,yahoo_close_exact,selected_close_exact,classification,
                   reason)
               VALUES ('future',?,?,?,?,?,?,?,?,?,?,?)""",
            [fixture[0]["contract_key"], fixture[0]["terms_note"], fixture[0]["expiry_session"],
             cutoff + timedelta(days=1), status,
             fixture[0]["label_source"] if status == "valid" else None,
             "90", "90", "90" if status == "valid" else None,
             "otm" if status == "valid" else None,
             None if status == "valid" else "close_sources_conflict"],
        )
    assert build_calibration(ledger, cutoff) == before


def test_calibration_cutoff_selects_latest_revision_at_or_before_boundary(tmp_path):
    ledger = ForecastLedger(tmp_path)
    digest = ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    ledger.record(issue(digest))
    first = datetime.combine(EXPIRY, datetime.min.time(), UTC) + timedelta(days=1)
    second = first + timedelta(days=1)
    ledger.record_label(label(checked_at=first))
    ledger.record_label(label(checked_at=second, status="excluded"))
    since = T0.date()
    assert len(ledger.calibration_rows(since=since, label_as_of=first)) == 1
    assert ledger.calibration_rows(since=since, label_as_of=first - timedelta(seconds=1)) == []
    assert ledger.calibration_rows(since=since, label_as_of=second) == []
    assert ledger.calibration_rows(since=since) == []
    eastern = timezone(timedelta(hours=-5))
    assert len(ledger.calibration_rows(since=since, label_as_of=first.astimezone(eastern))) == 1
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.calibration_rows(since=since, label_as_of=first.replace(tzinfo=None))
    tied = label(checked_at=second, close="40").checked()
    excluded = label(checked_at=second, status="excluded").checked()
    ledger.record_label(tied)
    chosen = max((tied, excluded), key=lambda revision: revision.idempotency_key)
    found = ledger.calibration_rows(since=since, label_as_of=second)
    assert bool(found) == (chosen.status == "valid")
    if found:
        assert found[0]["selected_close_exact"] == chosen.selected_close_exact


@pytest.mark.parametrize("failed", [False, True])
def test_terminal_job_replacement_keeps_new_coalescing_registration(tmp_path, failed):
    manager = JobManager(tmp_path)
    terminal = threading.Event()
    cleanup = threading.Event()
    running = threading.Event()
    release = threading.Event()
    original = manager._run

    def delayed_cleanup(job_id, worker):
        original(job_id, worker)
        if not terminal.is_set():
            terminal.set()
            assert cleanup.wait(5)

    def first_worker(_progress):
        if failed:
            raise RuntimeError("recorded failure")

    def replacement_worker(_progress):
        running.set()
        assert release.wait(5)

    manager._run = delayed_cleanup
    try:
        first = manager.submit("watch_refresh", first_worker, coalesce_key="refresh")
        assert terminal.wait(3)
        assert manager.get(first.id).state == ("failed" if failed else "succeeded")
        replacement = manager.submit("watch_refresh", replacement_worker, coalesce_key="refresh")
        assert replacement.id != first.id
        assert manager.submit("watch_refresh", replacement_worker,
                              coalesce_key="refresh").id == replacement.id
        cleanup.set()
        assert running.wait(3)
        assert manager.submit("watch_refresh", replacement_worker,
                              coalesce_key="refresh").id == replacement.id
    finally:
        cleanup.set()
        release.set()
        assert manager.wait(5)
    assert manager.get(replacement.id).state == "succeeded"


@pytest.mark.parametrize("period,empty", [
    ("screen", False), ("holdout", False), ("screen", True), ("holdout", True), ("all", False),
])
def test_replay_fit_coverage_matches_period_and_cutoff(tmp_path, monkeypatch, period, empty):
    script = Path(__file__).parents[1] / "scripts" / "evaluate_predictive.py"
    spec = spec_from_file_location("replay_period_regression", script)
    evaluator = module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    calendar = SessionCalendar()
    cutoff = date(2026, 9, 1)
    # Before cutoff, expiry exactly at cutoff, origin exactly at cutoff, and later.
    origins = [calendar.offset(cutoff, -2), calendar.offset(cutoff, -1), cutoff,
               calendar.offset(cutoff, 2)]
    snapshot = tmp_path / "frozen.parquet"
    _bars(800, date(2026, 9, 25)).write_parquet(snapshot)
    cohort = AuditCohort(date(2026, 9, 25), datetime(2026, 9, 26, tzinfo=UTC),
                         (AuditMember("AAPL", "0" * 64, T0, snapshot),))
    monkeypatch.setattr(evaluator, "read_audit_cohort", lambda _path: cohort)
    monkeypatch.setattr(evaluator, "_BANDS", {"1": range(1, 2)})
    monkeypatch.setattr(evaluator, "_STRIKE_GRID", (0.0,))
    # Preserve production origin slicing while supplying a deterministic schedule.
    schedule = [origins[0]] * (26 * 3 + 26)
    for index, origin in enumerate(origins):
        schedule[index * 26] = origin
    monkeypatch.setattr(calendar, "sessions", lambda _start, _end: schedule)
    if empty:
        cutoff = date(2026, 8, 1) if period == "screen" else date(2026, 10, 1)
    expected = [] if empty else origins[:1] if period in {"screen", "all"} else origins[2:]
    invoked = []

    def forecasts(_self, ticker, when, expiry):
        invoked.append(when.date())
        if when.date() not in expected:
            return {"lognormal_ewma": ShadowForecast(None, "excluded-period", 0, 0)}
        distribution = PredictiveDistribution(
            ticker=ticker, status="available", reason=None, method="lognormal_ewma",
            as_of=when.date(), expiry_session=expiry, horizon_sessions=1, spot=100,
            daily_volatility=0.02, model_version="test", support=60, data_hash="a" * 64,
            terminal_prices=(95.0, 105.0), weights=(0.5, 0.5),
        )
        return {"lognormal_ewma": ShadowForecast(distribution, None, 0, 1),
                "student_t_ewma": ShadowForecast(
                    replace(distribution, method="student_t_ewma"), None, 1, 1)}

    monkeypatch.setattr(PhysicalShadowForecaster, "forecast_candidates", forecasts)
    report = evaluator._replay_cohort(
        tmp_path, calendar, "student_t_ewma", max_origins=4, max_tickers=1,
        period=period, holdout_start=cutoff,
    )
    band = report["bands"]["1"]
    assert report["period"] == ("screen" if period == "all" else period)
    assert invoked == expected
    assert band["replay_scheduled_units"] == len(expected)
    assert band["replay_baseline_available_units"] == len(expected)
    assert band["ticker_origin_horizon_units"] == len(expected)
    assert band["replay_rejection_reasons"] == report["skipped"] == {}
