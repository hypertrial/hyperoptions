"""Prospective shadow forecasts must leave complete, comparable issuance evidence."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from options_api.contract_identity import make_watch_key
from options_api.outcomes import TERMS_NOTE
from options_api.physical_shadow_capture import PhysicalShadowCapture
from stocksweeper.forecast.ledger import ForecastIssuance, ForecastLabel, ForecastLedger
from stocksweeper.forecast.physical_contest import ShadowForecast
from stocksweeper.forecast.physical_evaluation import evaluate_band
from stocksweeper.forecast.predictive import PredictiveDistribution
from stocksweeper.forecast.promotion import _contest_rows


INPUT = date(2026, 9, 25)
EXPIRY = date(2026, 10, 2)
RETRIEVED = datetime(2026, 9, 25, 21, tzinfo=UTC)
ISSUED = datetime(2026, 9, 26, 12, tzinfo=UTC)
METHODS = {"lognormal_ewma", "empirical_scaled", "student_t_ewma", "gjr_garch_t"}


def _distribution(
    method: str = "empirical_scaled", *, digest: str = "a" * 64
) -> PredictiveDistribution:
    return PredictiveDistribution(
        ticker="IREN",
        status="available",
        reason=None,
        method=method,
        as_of=INPUT,
        expiry_session=EXPIRY,
        horizon_sessions=5,
        spot=100.0,
        daily_volatility=0.02,
        model_version=f"{method}-shadow-v1",
        support=60,
        data_hash=digest,
        terminal_prices=(80.0, 100.0, 120.0),
        weights=(0.2, 0.5, 0.3),
    )


def _issue(strike: str = "100.000") -> ForecastIssuance:
    return ForecastIssuance(
        contract_key=make_watch_key("IREN", "IREN", "call", EXPIRY.isoformat(), Decimal(strike)),
        ticker="IREN",
        root="IREN",
        side="call",
        expiration=EXPIRY,
        expiry_session=EXPIRY,
        strike_exact=strike,
        terms_note=TERMS_NOTE,
        contract_since=INPUT,
        input_session=INPUT,
        input_retrieved_at=RETRIEVED,
        issued_at=ISSUED,
        model_version="live-empirical-v1",
        method="empirical_scaled",
        data_hash="a" * 64,
        distribution_hash=None,
        price_basis="completed_close",
        spot_exact="100",
        status="available",
        itm_probability=0.3,
        otm_probability=0.2,
        atm_probability=0.5,
        unavailable_reason=None,
    )


def _capture(tmp_path, *, verified: bool = True) -> PhysicalShadowCapture:
    predictive = SimpleNamespace(
        forecaster=object(),
        ledger=ForecastLedger(tmp_path),
        cache_retrieved_at=lambda *_args: RETRIEVED if verified else None,
    )
    return PhysicalShadowCapture(predictive)


def _candidates(*, digest: str = "a" * 64) -> dict[str, ShadowForecast]:
    return {
        method: ShadowForecast(_distribution(method, digest=digest), None, 1.0, 1.0)
        for method in METHODS
    }


def test_live_empirical_still_captures_baseline_and_all_predeclared_challengers(tmp_path) -> None:
    capture = _capture(tmp_path)
    calls: list[tuple[object, ...]] = []

    def forecast(*args, **kwargs):
        calls.append((args, kwargs))
        return _candidates()

    capture.forecaster = SimpleNamespace(forecast_candidates=forecast)
    capture._capture_sync([(_issue(), _distribution())])

    rows = ForecastLedger(tmp_path).evaluation_rows()
    assert len(calls) == 1  # Fit once per ticker, input session, and expiry.
    assert {row["method"] for row in rows} == METHODS
    assert all(row["status"] == "available" for row in rows)
    assert all(row["input_session"] == INPUT and row["data_hash"] == "a" * 64 for row in rows)
    assert all(row["distribution_hash"] for row in rows)
    assert all(row["provenance"] == "as_issued" for row in rows)
    assert all(row["prepare_ms"] == 1.0 and row["lookup_ms"] == 1.0 for row in rows)
    baseline = next(row for row in rows if row["method"] == "lognormal_ewma")
    assert baseline["itm_probability"] == pytest.approx(0.5)
    assert baseline["otm_probability"] == pytest.approx(0.5)
    assert baseline["atm_probability"] == pytest.approx(0.0)
    empirical = next(row for row in rows if row["method"] == "empirical_scaled")
    assert empirical["itm_probability"] == pytest.approx(0.3)
    assert empirical["otm_probability"] == pytest.approx(0.2)
    assert empirical["atm_probability"] == pytest.approx(0.5)


def test_failed_shadow_issuance_cannot_enter_live_cache(tmp_path) -> None:
    capture = _capture(tmp_path)
    capture.forecaster = SimpleNamespace(
        forecast_candidates=lambda *_args, **_kwargs: _candidates()
    )

    def failed_record(_entries):
        raise OSError("ledger unavailable")

    capture.predictive.ledger.record_batch = failed_record
    with pytest.raises(OSError, match="ledger unavailable"):
        capture._capture_sync([(_issue(), _distribution())])
    assert not capture._live


def test_as_issued_report_includes_persisted_preparation_and_lookup_latency(tmp_path) -> None:
    capture = _capture(tmp_path)
    candidates = _candidates()
    candidates["student_t_ewma"] = ShadowForecast(
        _distribution("student_t_ewma"), None, 7.0, 3.0
    )
    capture.forecaster = SimpleNamespace(forecast_candidates=lambda *_args, **_kwargs: candidates)
    capture._capture_sync([(_issue(), _distribution())])
    ledger = ForecastLedger(tmp_path)
    ledger.record_label(
        ForecastLabel(
            contract_key=_issue().contract_key,
            terms_note=TERMS_NOTE,
            expiry_session=EXPIRY,
            checked_at=datetime(2026, 10, 3, 12, tzinfo=UTC),
            status="valid",
            reason=None,
            source="Nasdaq historical Close (Yahoo cross-check)",
            nasdaq_close_exact="100.000",
            yahoo_close_exact="100.000",
            selected_close_exact="100.000",
            classification="atm",
        )
    )
    rows = _contest_rows(
        ledger, since=INPUT, as_of=datetime(2026, 10, 4, tzinfo=UTC),
        band="2-5", methods=("lognormal_ewma", "student_t_ewma"),
    )
    report = evaluate_band(
        rows, "student_t_ewma", "2-5", holdout_start=INPUT,
        period="holdout", bootstrap_samples=100,
    )
    assert report["ticker_origin_horizon_units"] == 1
    assert report["latency_ms"] == {
        "prepare": {"p50": 7.0, "p95": 7.0},
        "lookup": {"p50": 3.0, "p95": 3.0},
    }


def test_one_challenger_rejection_does_not_erase_available_baseline(tmp_path) -> None:
    capture = _capture(tmp_path)
    candidates = _candidates()
    candidates["gjr_garch_t"] = ShadowForecast(None, "gjr_nonconverged", 5.0, 0.0)
    capture.forecaster = SimpleNamespace(forecast_candidates=lambda *_args, **_kwargs: candidates)
    capture._capture_sync([(_issue(), _distribution())])

    rows = {row["method"]: row for row in ForecastLedger(tmp_path).evaluation_rows()}
    assert rows["lognormal_ewma"]["status"] == "available"
    assert rows["student_t_ewma"]["status"] == "available"
    assert rows["gjr_garch_t"]["status"] == "unavailable"
    assert rows["gjr_garch_t"]["unavailable_reason"] == "gjr_nonconverged"


def test_shadow_failure_cannot_mask_valid_live_method(tmp_path) -> None:
    capture = _capture(tmp_path)
    base = _distribution("empirical_scaled")
    key = base.ticker, base.as_of, EXPIRY, INPUT, base.data_hash
    capture._live[key] = (
        {"empirical_scaled": ShadowForecast(None, "market_data_missing", 0, 0)},
        None,
    )

    result = capture.candidate(base, "empirical_scaled", EXPIRY, INPUT)
    assert result.distribution is base
    assert result.reason is None
    assert capture.candidate(base, "student_t_ewma", EXPIRY, INPUT).reason == (
        "candidate_not_prepared"
    )


@pytest.mark.parametrize("changed", ("hash", "session", "expiry", "contract_since"))
def test_stale_shadow_candidate_is_not_reused_for_new_input(tmp_path, changed: str) -> None:
    capture = _capture(tmp_path)
    old = _distribution("empirical_scaled")
    key = old.ticker, old.as_of, EXPIRY, INPUT, old.data_hash
    capture._live[key] = (
        {"student_t_ewma": ShadowForecast(_distribution("student_t_ewma"), None, 0, 0)},
        None,
    )

    current = old
    expiry = EXPIRY
    contract_since = INPUT
    if changed == "hash":
        current = replace(old, data_hash="b" * 64)
    elif changed == "session":
        current = replace(old, as_of=date(2026, 9, 26))
    elif changed == "expiry":
        expiry = date(2026, 10, 9)
        current = replace(old, expiry_session=expiry)
    else:
        contract_since = date(2026, 9, 24)

    assert (
        capture.candidate(current, "empirical_scaled", expiry, contract_since).distribution
        is current
    )
    shadow = capture.candidate(current, "student_t_ewma", expiry, contract_since)
    assert shadow.distribution is None
    assert shadow.reason == "candidate_not_prepared"


@pytest.mark.parametrize(
    ("base", "reason"),
    (
        (replace(_distribution(), status="unavailable", reason="source_outage"), "source_outage"),
        (replace(_distribution(), data_hash=None), "completed_close_forecast_unavailable"),
    ),
)
def test_invalid_base_cannot_bypass_availability_guard(
    tmp_path, base: PredictiveDistribution, reason: str
) -> None:
    capture = _capture(tmp_path)
    result = capture.candidate(base, base.method, EXPIRY, INPUT)
    assert result.distribution is None
    assert result.reason == reason


def test_unavailable_first_contract_does_not_suppress_valid_peer(tmp_path) -> None:
    capture = _capture(tmp_path)
    capture.forecaster = SimpleNamespace(
        forecast_candidates=lambda *_args, **_kwargs: _candidates()
    )
    contracts = sorted(
        (_issue("100.000"), _issue("110.000")),
        key=lambda item: hashlib.sha256(item.contract_key.encode()).digest(),
    )
    unavailable = replace(
        contracts[0],
        status="unavailable",
        itm_probability=None,
        otm_probability=None,
        atm_probability=None,
        unavailable_reason="source_outage",
    )
    capture._capture_sync([(unavailable, None), (contracts[1], _distribution())])
    rows = ForecastLedger(tmp_path).evaluation_rows()
    assert sum(row["status"] == "available" for row in rows) == 4
    assert {row["unavailable_reason"] for row in rows if row["status"] == "unavailable"} == {
        "input_vintage_changed"
    }


def test_manifest_mismatch_records_four_unavailable_attempts_without_fitting(tmp_path) -> None:
    capture = _capture(tmp_path, verified=False)
    capture.forecaster = SimpleNamespace(
        forecast_candidates=lambda *_args, **_kwargs: pytest.fail("unverified cache was fitted")
    )
    capture._capture_sync([(_issue(), _distribution())])

    rows = ForecastLedger(tmp_path).evaluation_rows()
    assert {row["method"] for row in rows} == METHODS
    assert all(row["status"] == "unavailable" for row in rows)
    assert all(row["unavailable_reason"] == "input_provenance_unverified" for row in rows)
    assert all(row["itm_probability"] is None and row["distribution_hash"] is None for row in rows)
    assert capture.candidate(
        _distribution("lognormal_ewma"), "student_t_ewma", EXPIRY, INPUT
    ).reason == "input_provenance_unverified"


def test_changed_input_vintage_is_rejected_even_if_fit_succeeds(tmp_path) -> None:
    capture = _capture(tmp_path)
    capture.forecaster = SimpleNamespace(
        forecast_candidates=lambda *_args, **_kwargs: _candidates(digest="b" * 64)
    )
    capture._capture_sync([(_issue(), _distribution())])

    rows = ForecastLedger(tmp_path).evaluation_rows()
    assert len(rows) == 4
    assert all(row["status"] == "unavailable" for row in rows)
    assert all(row["unavailable_reason"] == "input_vintage_changed" for row in rows)


def test_candidate_fit_error_is_recorded_as_fit_failure_not_bad_provenance(tmp_path) -> None:
    capture = _capture(tmp_path)

    def broken_fit(*_args, **_kwargs):
        raise RuntimeError("optimizer failed")

    capture.forecaster = SimpleNamespace(forecast_candidates=broken_fit)
    capture._capture_sync([(_issue(), _distribution())])

    rows = ForecastLedger(tmp_path).evaluation_rows()
    assert {row["method"] for row in rows} == METHODS
    assert all(row["status"] == "unavailable" for row in rows)
    assert all(row["unavailable_reason"] == "shadow_fit_failed" for row in rows)
    assert capture.candidate(
        _distribution("lognormal_ewma"), "student_t_ewma", EXPIRY, INPUT
    ).reason == "shadow_fit_failed"
    capture.forecaster = SimpleNamespace(
        forecast_candidates=lambda *_args, **_kwargs: _candidates()
    )
    capture._capture_sync([(_issue(), _distribution())])
    assert capture.candidate(
        _distribution("lognormal_ewma"), "student_t_ewma", EXPIRY, INPUT
    ).distribution is not None


def test_restart_does_not_duplicate_the_same_shadow_issuances(tmp_path) -> None:
    entries = [(_issue(), _distribution())]
    for _ in range(2):
        capture = _capture(tmp_path)
        capture.forecaster = SimpleNamespace(
            forecast_candidates=lambda *_args, **_kwargs: _candidates()
        )
        capture._capture_sync(entries)

    rows = ForecastLedger(tmp_path).evaluation_rows()
    assert len(rows) == 4
    assert len({row["idempotency_key"] for row in rows}) == 4


def test_contracts_over_fit_capacity_leave_recorded_unavailable_attempts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("options_api.physical_shadow_capture._MAX_BATCH_CONTRACTS", 1)
    capture = _capture(tmp_path)
    capture.forecaster = SimpleNamespace(
        forecast_candidates=lambda *_args, **_kwargs: _candidates()
    )
    contracts = [
        (_issue("100.000"), _distribution()),
        (replace(_issue("110.000"), contract_since=date(2026, 9, 24)), _distribution()),
    ]
    capture._capture_sync(contracts)

    rows = ForecastLedger(tmp_path).evaluation_rows()
    assert len(rows) == 8
    assert {row["contract_key"] for row in rows} == {
        _issue("100.000").contract_key,
        _issue("110.000").contract_key,
    }
    assert len([row for row in rows if row["status"] == "available"]) == 4
    assert (
        len([row for row in rows if row["unavailable_reason"] == "shadow_capacity_exceeded"]) == 4
    )
    skipped = max(
        (issue for issue, _ in contracts),
        key=lambda issue: hashlib.sha256(issue.contract_key.encode()).digest(),
    )
    assert capture.candidate(
        _distribution("lognormal_ewma"), "student_t_ewma", EXPIRY, skipped.contract_since
    ).reason == "shadow_capacity_exceeded"


@pytest.mark.asyncio
async def test_full_pending_queue_retains_failed_candidate_attempts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("options_api.physical_shadow_capture._MAX_PENDING_BATCHES", 1)
    capture = _capture(tmp_path)
    gate = asyncio.Event()

    async def blocked(_entries, _quotes):
        await gate.wait()

    capture._capture = blocked
    record_capacity = capture._record_capacity
    capacity_threads: list[int] = []

    def record_off_loop(entries):
        capacity_threads.append(threading.get_ident())
        record_capacity(entries)

    capture._record_capacity = record_off_loop
    capture.submit([(_issue("100.000"), _distribution())])
    await asyncio.sleep(0)
    capture.submit([(_issue("110.000"), _distribution())])
    assert capacity_threads == []
    gate.set()
    await capture.close()
    assert capacity_threads and capacity_threads[0] != threading.get_ident()

    rejected = [
        row
        for row in ForecastLedger(tmp_path).evaluation_rows()
        if row["contract_key"] == _issue("110.000").contract_key
    ]
    assert {row["method"] for row in rejected} == METHODS
    assert all(row["status"] == "unavailable" for row in rejected)
    assert all(row["unavailable_reason"] == "shadow_capacity_exceeded" for row in rejected)
    assert capture.candidate(
        _distribution("lognormal_ewma"), "student_t_ewma", EXPIRY, INPUT
    ).reason == "shadow_capacity_exceeded"


@pytest.mark.asyncio
async def test_capacity_rejection_can_refit_same_snapshot_after_bounded_retry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("options_api.physical_shadow_capture._MAX_PENDING_BATCHES", 1)
    clock = [0.0]
    monkeypatch.setattr("options_api.physical_shadow_capture.monotonic", lambda: clock[0])
    capture = _capture(tmp_path)
    calls: list[str] = []

    async def record_capacity(_entries, _quotes):
        calls.append("capacity")

    async def fit(_entries, _quotes):
        calls.append("fit")

    capture._record_capacity_async = record_capacity
    capture._capture = fit
    placeholder = asyncio.create_task(asyncio.sleep(0))
    capture._fit_tasks.add(placeholder)
    entries = [(_issue(), _distribution())]
    capture.submit(entries)
    await asyncio.sleep(0)
    capture._fit_tasks.discard(placeholder)
    capture.submit(entries)
    await asyncio.sleep(0)
    assert calls == ["capacity"]
    clock[0] = 31.0
    capture.submit(entries)
    await capture.close()
    assert calls == ["capacity", "fit"]


@pytest.mark.asyncio
async def test_failed_submit_retries_then_success_dedupes_same_snapshot(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr("options_api.physical_shadow_capture.monotonic", lambda: clock[0])
    capture = _capture(tmp_path)
    calls = [0]

    def fit(*_args, **_kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("temporary optimizer failure")
        return _candidates()

    capture.forecaster = SimpleNamespace(forecast_candidates=fit)
    entries = [(_issue(), _distribution("lognormal_ewma"))]
    capture.submit(entries)
    await asyncio.gather(*capture._tasks)
    await asyncio.sleep(0)
    assert calls[0] == 1
    assert capture.candidate(
        _distribution("lognormal_ewma"), "student_t_ewma", EXPIRY, INPUT
    ).reason == "shadow_fit_failed"

    clock[0] = 31.0
    capture.submit(entries)
    await asyncio.gather(*capture._tasks)
    await asyncio.sleep(0)
    assert calls[0] == 2
    assert capture.candidate(
        _distribution("lognormal_ewma"), "student_t_ewma", EXPIRY, INPUT
    ).distribution is not None

    clock[0] = 62.0
    capture.submit(entries)
    await asyncio.sleep(0)
    assert calls[0] == 2
    await capture.close()
