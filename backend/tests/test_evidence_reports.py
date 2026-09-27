"""Dated comparison evidence stays descriptive when outcomes are scarce."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from stocksweeper.forecast.evidence_reports import (
    CANDIDATE_VERSIONS,
    build_model_evidence,
    ledger_band_rows,
    load_replay_report,
    save_replay_report,
)
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.physical_evaluation import evaluate_band
from stocksweeper.forecast.predictive import BASELINE_VERSION


def test_unlabelled_ledger_reports_zero_and_no_significance(tmp_path):
    evidence = build_model_evidence(
        tmp_path, ForecastLedger(tmp_path), datetime(2026, 9, 27, tzinfo=UTC)
    )
    assert len(evidence) == 15
    for (method, band), result in evidence.items():
        assert method and band
        prospective = result["prospective"]
        assert prospective["provenance"].startswith("as_issued")
        assert prospective["ticker_origin_horizon_units"] == 0
        assert prospective["brier"]["candidate"] is None
        assert prospective["brier"]["bootstrap_95"] is None
        assert prospective["calibration_by_side"]["call"][0]["count"] == 0
        assert prospective["calibration_by_side"]["put"][0]["count"] == 0
        assert result["retrospective"] is None


def test_replay_report_is_explicitly_current_vintage_and_detects_tampering(tmp_path):
    band = evaluate_band([], "student_t_ewma", "1", bootstrap_samples=100)
    report = {"provenance": "immutable_replay", "bands": {"1": band}}
    path = save_replay_report(tmp_path, "student_t_ewma", report)
    loaded = load_replay_report(tmp_path, "student_t_ewma")
    assert loaded["report"] == report
    assert loaded["report_hash"]
    assert loaded["schema_version"] == 2

    payload = json.loads(path.read_text())
    payload["report"]["bands"]["1"]["tickers"] = 99
    path.write_text(json.dumps(payload))
    assert load_replay_report(tmp_path, "student_t_ewma") is None


def test_ledger_evidence_requires_mature_label_and_as_of_snapshot():
    issued = datetime(2026, 9, 25, 22, tzinfo=UTC)
    cutoff = datetime(2026, 9, 28, 22, tzinfo=UTC)
    kwargs_seen = {}

    class Ledger:
        def iter_evaluation_rows(self, **kwargs):
            kwargs_seen.update(kwargs)
            current = {
                "ticker": "TEST", "input_session": date(2026, 9, 25),
                "expiration": date(2026, 9, 28), "expiry_session": date(2026, 9, 28),
                "method": "student_t_ewma", "label_status": "valid",
                "model_version": CANDIDATE_VERSIONS["student_t_ewma"],
                "label_checked_at": datetime(2026, 9, 28, 19, tzinfo=UTC),
                "issued_at": issued, "spot_exact": "99", "strike_exact": "100",
                "side": "call", "status": "available", "itm_probability": 0.6,
                "observed_itm": True, "volatility_regime": "unknown",
                "known_event_status": "unknown", "data_hash": "a" * 64,
                "idempotency_key": "one", "contract_key": "one",
                "unavailable_reason": None, "label_reason": None, "crps": None,
                "prepare_ms": None, "lookup_ms": None,
            }
            yield {**current, "model_version": "obsolete-v0", "idempotency_key": "old"}
            yield current
            yield {
                **current, "status": "unavailable", "itm_probability": None,
                "idempotency_key": "unavailable", "unavailable_reason": "student_fit_failed",
            }

    rows = ledger_band_rows(
        Ledger(), SessionCalendar(), "as_issued", "student_t_ewma", None, "all", "1",
        as_of=cutoff, current_version_only=True,
    )
    assert len(rows) == 2
    assert rows[0].observed_itm is None
    assert rows[1].reason == "student_fit_failed"
    assert kwargs_seen["issued_before"] == cutoff
    assert kwargs_seen["label_as_of"] == cutoff


def test_current_version_coverage_excludes_old_candidate_attempts() -> None:
    origin = date(2026, 9, 25)
    expiry = date(2026, 9, 28)
    issued = datetime(2026, 9, 25, 20, tzinfo=UTC)
    common = {
        "ticker": "TEST", "input_session": origin, "expiration": expiry,
        "expiry_session": expiry, "strike_exact": "100", "spot_exact": "99",
        "side": "call", "label_status": None, "label_checked_at": None,
        "issued_at": issued, "volatility_regime": "unknown",
        "known_event_status": "unknown", "data_hash": "a" * 64,
        "label_reason": None, "crps": None, "prepare_ms": None, "lookup_ms": None,
    }
    rows = [
        {
            **common, "contract_key": contract, "idempotency_key": f"{contract}-{method}",
            "method": method, "model_version": version, "status": status,
            "itm_probability": 0.6 if status == "available" else None,
            "unavailable_reason": None if status == "available" else "student_fit_failed",
        }
        for contract, candidate_version in (
            ("old", "student_t_ewma-shadow-v1"),
            ("current", CANDIDATE_VERSIONS["student_t_ewma"]),
        )
        for method, version, status in (
            ("lognormal_ewma", BASELINE_VERSION, "available"),
            ("student_t_ewma", candidate_version, "unavailable"),
        )
    ]
    rows.append({
        **rows[-1], "contract_key": "candidate-only", "idempotency_key": "candidate-only",
    })

    class Ledger:
        def iter_evaluation_rows(self, **_kwargs):
            return iter(rows)

    current = ledger_band_rows(
        Ledger(), SessionCalendar(), "as_issued", "student_t_ewma", None, "all", "1",
        current_version_only=True,
    )
    assert {row.contract_id for row in current} == {"current", "candidate-only"}
    report = evaluate_band(current, "student_t_ewma", "1", bootstrap_samples=100)
    assert report["contract_cells_attempted"] == 2
    assert report["rejection_reasons"] == {
        "student_fit_failed": 1, "baseline_not_issued": 1,
    }
