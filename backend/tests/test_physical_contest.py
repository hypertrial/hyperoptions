"""Model reproducibility, causal fit reuse, and paired accuracy evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from importlib.util import module_from_spec, spec_from_file_location
from math import isclose
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.evidence_reports import ledger_contest
from stocksweeper.forecast.audit import AuditCohort, AuditMember
from stocksweeper.forecast.physical_contest import (
    PhysicalShadowForecaster,
    ShadowForecast,
    _fit_gjr,
    _gjr_terminal,
    _student_terminal,
)
from stocksweeper.forecast.physical_evaluation import ContestRow, crps, evaluate_band
from stocksweeper.forecast.market import clean_completed, price_hash
from stocksweeper.forecast.predictive import PredictiveDistribution, PredictiveForecaster


class _Prices:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame

    def fetch(self, ticker, start, end):
        return self.frame.filter(pl.col("ts") <= end)


def _bars(count: int, last: date) -> pl.DataFrame:
    sessions = SessionCalendar().sessions(date(2020, 1, 1), last)[-count:]
    returns = np.random.default_rng(17).standard_t(5, count) * 0.012
    closes = (100 * np.exp(np.cumsum(returns))).tolist()
    return pl.DataFrame(
        {
            "ts": sessions,
            "open": closes,
            "high": [price * 1.01 for price in closes],
            "low": [price * 0.99 for price in closes],
            "close": closes,
            "volume": [1000.0] * count,
            "dividends": [0.0] * count,
            "stock_splits": [0.0] * count,
        }
    )


def test_shadow_scenarios_are_deterministic_and_use_one_fit_per_input_session(
    tmp_path, monkeypatch
):
    session = date(2026, 9, 25)
    forecaster = PredictiveForecaster(tmp_path, _Prices(_bars(220, session)))
    forecaster.prepare("AAPL", session)
    shadow = PhysicalShadowForecaster(forecaster)
    calls = {"student": 0, "gjr": 0}

    def student(returns, splits):
        calls["student"] += 1
        return (5.0, 0.8), None

    def gjr(returns, splits):
        calls["gjr"] += 1
        return (0.01, 0.05, 0.04, 0.90, 6.0, 1.0), None

    monkeypatch.setattr("stocksweeper.forecast.physical_contest._fit_student", student)
    monkeypatch.setattr("stocksweeper.forecast.physical_contest._fit_gjr", gjr)
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    first = shadow.forecast_candidates("AAPL", now, date(2026, 10, 2))
    second = shadow.forecast_candidates("AAPL", now, date(2026, 10, 9))
    repeated = shadow.forecast_candidates("AAPL", now, date(2026, 10, 2))
    assert calls == {"student": 1, "gjr": 1}
    for name in ("student_t_ewma", "gjr_garch_t"):
        distribution = first[name].distribution
        assert distribution is not None
        assert len(distribution.prices) == 4096
        assert distribution.prices == repeated[name].distribution.prices
        strike = distribution.spot
        assert isclose(
            distribution.probability("call", strike) + distribution.probability("put", strike),
            1.0,
            abs_tol=1e-12,
        )
        assert second[name].distribution.expiry_session == date(2026, 10, 9)
    assert first["lognormal_ewma"].distribution.method == "lognormal_ewma"


def test_gjr_requires_long_split_safe_history_and_scenarios_are_seeded():
    short = np.random.default_rng(0).normal(0, 0.01, 499)
    assert _fit_gjr(short, np.zeros(499, dtype=bool))[1] == "gjr_history_short"
    long = np.random.default_rng(0).normal(0, 0.01, 500)
    splits = np.zeros(500, dtype=bool)
    splits[25] = True
    assert _fit_gjr(long, splits)[1] == "gjr_split_in_window"
    params = (0.01, 0.05, 0.04, 0.9, 6.0, 1.0)
    assert _gjr_terminal(100, 3, params, -0.01, 4) == _gjr_terminal(100, 3, params, -0.01, 4)
    assert _student_terminal(100, 0.02, 3, (5, 0.8), 4) == _student_terminal(
        100, 0.02, 3, (5, 0.8), 4
    )


def test_invalid_candidate_scenarios_leave_baseline_available(tmp_path, monkeypatch):
    session = date(2026, 9, 25)
    forecaster = PredictiveForecaster(tmp_path, _Prices(_bars(220, session)))
    forecaster.prepare("AAPL", session)
    monkeypatch.setattr(
        "stocksweeper.forecast.physical_contest._fit_student",
        lambda returns, splits: ((5.0, 1.0), None),
    )

    def invalid(*args):
        raise ValueError("overflow")

    monkeypatch.setattr("stocksweeper.forecast.physical_contest._student_terminal", invalid)
    result = PhysicalShadowForecaster(forecaster).forecast_candidates(
        "AAPL", datetime(2026, 9, 26, 12, tzinfo=UTC), date(2026, 10, 2)
    )
    assert result["lognormal_ewma"].distribution is not None
    assert result["student_t_ewma"].distribution is None
    assert result["student_t_ewma"].reason == "candidate_scenarios_invalid"


def test_cached_candidate_never_fits_or_builds_unselected_model(tmp_path, monkeypatch):
    session = date(2026, 9, 25)
    forecaster = PredictiveForecaster(tmp_path, _Prices(_bars(220, session)))
    forecaster.prepare("AAPL", session)
    shadow = PhysicalShadowForecaster(forecaster)
    current = forecaster.forecast("AAPL", datetime(2026, 9, 26, 12, tzinfo=UTC), date(2026, 10, 2))
    assert shadow.cached_candidate(current, "student_t_ewma").reason == ("candidate_not_prepared")
    clean = clean_completed(forecaster.prices.read("AAPL"), session, forecaster.calendar)
    shadow._fit("AAPL", session, clean, price_hash(clean))
    monkeypatch.setattr(shadow, "_fit", lambda *args: pytest.fail("lookup refit"))
    monkeypatch.setattr(
        "stocksweeper.forecast.physical_contest._gjr_terminal",
        lambda *args: pytest.fail("unselected GJR scenarios"),
    )
    selected = shadow.cached_candidate(current, "student_t_ewma", clean=clean)
    assert selected.distribution is not None
    assert selected.distribution.method == "student_t_ewma"


def test_crps_weighted_scenarios_and_invalid_weights():
    assert crps((1.0, 3.0), (0.5, 0.5), 2.0) == pytest.approx(0.5)
    with pytest.raises(ValueError, match="sum to one"):
        crps((1.0, 3.0), (0.4, 0.5), 2.0)


def _paired_rows(provenance: str = "as_issued") -> list[ContestRow]:
    calendar = SessionCalendar()
    origins = calendar.sessions(date(2026, 1, 2), date(2026, 5, 29))[::2][:25]
    rows = []
    for ticker_index in range(20):
        ticker = f"T{ticker_index}"
        for origin in origins:
            expiry = calendar.offset(origin, 1)
            for side in ("call", "put"):
                observed = side == "call"
                base = ContestRow(
                    ticker=ticker,
                    origin=origin,
                    expiry_session=expiry,
                    horizon=1,
                    strike="100",
                    side=side,
                    method="lognormal_ewma",
                    probability=0.6 if observed else 0.4,
                    observed_itm=observed,
                    provenance=provenance,
                    moneyness="near_atm",
                    input_vintage=f"{ticker}|{origin}",
                    issued_at=datetime.combine(origin, datetime.min.time(), tzinfo=UTC),
                    crps=1.0,
                )
                rows.extend(
                    (
                        base,
                        replace(
                            base,
                            method="student_t_ewma",
                            probability=0.8 if observed else 0.2,
                            crps=0.8,
                            prepare_ms=25,
                            lookup_ms=2,
                        ),
                    )
                )
    return rows


def test_paired_band_evidence_uses_ticker_origin_units_and_provenance():
    rows = _paired_rows()
    descriptive = evaluate_band(rows, "student_t_ewma", "1", bootstrap_samples=200)
    assert descriptive["provenance"] == ["as_issued"]
    report = evaluate_band(
        rows,
        "student_t_ewma",
        "1",
        holdout_start=date(2025, 12, 31),
        period="holdout",
        bootstrap_samples=200,
    )
    assert report["tickers"] == 20
    assert report["independent_date_blocks"] == 25
    assert report["ticker_origin_horizon_units"] == 500
    assert report["contract_forecasts_available"] == 1000
    assert report["brier"]["bootstrap_95"][1] < 0
    assert report["crps"]["scored_units"] == 500
    assert report["brier"]["paired_delta"] < 0
    call_bins = report["calibration_by_side"]["call"]
    put_bins = report["calibration_by_side"]["put"]
    assert sum(item["count"] for item in call_bins) == 500
    assert sum(item["count"] for item in put_bins) == 500
    assert next(item for item in call_bins if item["count"])["observed_rate"] == 1
    assert next(item for item in put_bins if item["count"])["observed_rate"] == 0
    replay = evaluate_band(
        _paired_rows("immutable_replay"),
        "student_t_ewma",
        "1",
        holdout_start=date(2025, 12, 31),
        period="holdout",
        bootstrap_samples=200,
    )
    assert replay["provenance"] == ["immutable_replay"]


def test_missing_candidate_and_duplicate_issuance_do_not_inflate_units():
    rows = _paired_rows()
    one = rows[1]
    missing = [row for row in rows if row is not one]
    report = evaluate_band(missing, "student_t_ewma", "1", bootstrap_samples=200)
    assert report["contract_forecasts_available"] < report["baseline_contract_forecasts_available"]
    assert report["rejection_reasons"]["candidate_not_issued"] == 1
    duplicate = replace(rows[0], issued_at=rows[0].issued_at.replace(hour=1), probability=0.1)
    original = evaluate_band(rows, "student_t_ewma", "1", bootstrap_samples=200)
    repeated = evaluate_band([*rows, duplicate], "student_t_ewma", "1", bootstrap_samples=200)
    assert repeated["ticker_origin_horizon_units"] == 500
    assert repeated["duplicate_attempts_excluded"] == 1
    assert repeated["brier"] == original["brier"]


def test_data_revision_is_not_paired_with_earlier_baseline_vintage():
    base, challenger = _paired_rows()[:2]
    revised = replace(
        challenger, input_vintage="revised-data", issued_at=base.issued_at.replace(hour=1)
    )
    later_baseline = replace(
        base,
        input_vintage="revised-data",
        issued_at=base.issued_at.replace(hour=1),
        probability=0.3,
    )
    report = evaluate_band(
        [base, later_baseline, revised], "student_t_ewma", "1", bootstrap_samples=200
    )
    assert report["ticker_origin_horizon_units"] == 0
    assert report["baseline_contract_forecasts_available"] == 1
    assert report["contract_forecasts_available"] == 0
    assert report["later_vintage_attempts_excluded"] == 2


def test_frozen_cohort_replay_is_screening_only(tmp_path, monkeypatch):
    script = Path(__file__).parents[1] / "scripts" / "evaluate_predictive.py"
    spec = spec_from_file_location("evaluate_predictive_script", script)
    assert spec is not None and spec.loader is not None
    evaluator = module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    completed = date(2026, 9, 25)
    snapshot = tmp_path / "frozen.parquet"
    _bars(800, completed).write_parquet(snapshot)
    cohort = AuditCohort(
        completed,
        datetime(2026, 9, 26, tzinfo=UTC),
        (AuditMember("AAPL", "0" * 64, datetime(2026, 9, 26, tzinfo=UTC), snapshot),),
    )
    monkeypatch.setattr(evaluator, "read_audit_cohort", lambda path: cohort)
    monkeypatch.setattr(evaluator, "_BANDS", {"1": range(1, 2)})
    monkeypatch.setattr(evaluator, "_STRIKE_GRID", (0.0,))

    def fake_candidates(self, ticker, when, expiry):
        origin = SessionCalendar().last_completed(when)
        baseline = PredictiveDistribution(
            ticker=ticker,
            status="available",
            reason=None,
            method="lognormal_ewma",
            as_of=origin,
            expiry_session=expiry,
            horizon_sessions=1,
            spot=100,
            daily_volatility=0.02,
            model_version="test",
            support=60,
            data_hash="a" * 64,
            terminal_prices=(95.0, 105.0),
            weights=(0.5, 0.5),
        )
        challenger = replace(baseline, method="student_t_ewma")
        return {
            "lognormal_ewma": ShadowForecast(baseline, None, 0, 1),
            "student_t_ewma": ShadowForecast(challenger, None, 1, 1),
        }

    monkeypatch.setattr(PhysicalShadowForecaster, "forecast_candidates", fake_candidates)
    report = evaluator._replay_cohort(
        tmp_path,
        SessionCalendar(),
        "student_t_ewma",
        max_origins=1,
        max_tickers=1,
        period="screen",
        holdout_start=date(2026, 10, 1),
    )
    assert report["provenance"] == "immutable_replay"
    assert report["bands"]["1"]["ticker_origin_horizon_units"] == 1
    assert report["bands"]["1"]["provenance"] == ["immutable_replay"]


def test_ledger_contest_uses_exact_joined_label_and_counts_failed_attempts():
    issued = datetime(2026, 9, 25, 22, tzinfo=UTC)
    common = {
        "ticker": "AAPL",
        "contract_key": "AAPL|2026-09-28|call|100",
        "input_session": date(2026, 9, 25),
        "expiration": date(2026, 9, 28),
        "expiry_session": date(2026, 9, 28),
        "side": "call",
        "strike_exact": "100",
        "spot_exact": "99.5",
        "issued_at": issued,
        "idempotency_key": "same-input",
        "data_hash": "a" * 64,
        "label_checked_at": datetime(2026, 9, 28, 22, tzinfo=UTC),
        "label_status": "valid",
        "label_reason": None,
        "selected_close_exact": "101.00",
        "observed_itm": True,
        "distribution_hash": "a" * 64,
        "volatility_regime": "medium",
        "known_event_status": "unknown",
        "prepare_ms": 12.0,
        "lookup_ms": 2.0,
    }

    class Ledger:
        def evaluation_rows(self, *, provenance):
            return [
                {
                    **common,
                    "method": "lognormal_ewma",
                    "status": "available",
                    "itm_probability": 0.6,
                    "unavailable_reason": None,
                },
                {
                    **common,
                    "method": "student_t_ewma",
                    "status": "unavailable",
                    "itm_probability": None,
                    "unavailable_reason": "gjr_nonconverged",
                    "distribution_hash": None,
                },
                ]

        def iter_evaluation_rows(self, **kwargs):
            assert kwargs["methods"] == ("lognormal_ewma", "student_t_ewma")
            for item in self.evaluation_rows(provenance=kwargs["provenance"]):
                yield {**item, "crps": 2.5 if item["distribution_hash"] else None}

        def panel_coverage(self, **_kwargs):
            return {"scope": "fixed_audit_cohort_panel_cells", "cohort_status": "not_frozen"}

        def evaluation_skipped_attempts(self, provenance):
            return {}

    report = ledger_contest(
        Ledger(), SessionCalendar(), "as_issued", "student_t_ewma", None, "all"
    )
    first = report["bands"]["1"]
    assert first["baseline_contract_forecasts_available"] == 1
    assert first["contract_forecasts_available"] == 0
    assert first["rejection_reasons"]["gjr_nonconverged"] == 1
    assert first["crps"]["scored_units"] == 0

    class LateLedger(Ledger):
        def evaluation_rows(self, *, provenance):
            late = {**common, "issued_at": datetime(2026, 9, 28, 21, tzinfo=UTC)}
            return [
                {
                    **late,
                    "method": "lognormal_ewma",
                    "status": "available",
                    "itm_probability": 0.6,
                    "unavailable_reason": None,
                },
                {
                    **late,
                    "method": "student_t_ewma",
                    "status": "available",
                    "itm_probability": 0.8,
                    "unavailable_reason": None,
                },
            ]

    late_report = ledger_contest(
        LateLedger(), SessionCalendar(), "as_issued", "student_t_ewma", None, "all"
    )
    assert late_report["bands"]["1"]["ticker_origin_horizon_units"] == 0
