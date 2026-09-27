"""Dated comparison evidence stays descriptive when outcomes are scarce."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from stocksweeper.forecast.evidence_reports import (
    build_model_evidence,
    ledger_band_rows,
    load_replay_report,
    save_replay_report,
)
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.physical_evaluation import evaluate_band


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
            yield {
                "ticker": "TEST", "input_session": date(2026, 9, 25),
                "expiration": date(2026, 9, 28), "expiry_session": date(2026, 9, 28),
                "method": "student_t_ewma", "label_status": "valid",
                "label_checked_at": datetime(2026, 9, 28, 19, tzinfo=UTC),
                "issued_at": issued, "spot_exact": "99", "strike_exact": "100",
                "side": "call", "status": "available", "itm_probability": 0.6,
                "observed_itm": True, "volatility_regime": "unknown",
                "known_event_status": "unknown", "data_hash": "a" * 64,
                "idempotency_key": "one", "contract_key": "one",
                "unavailable_reason": None, "label_reason": None, "crps": None,
                "prepare_ms": None, "lookup_ms": None,
            }

    rows = ledger_band_rows(
        Ledger(), SessionCalendar(), "as_issued", "student_t_ewma", None, "all", "1",
        as_of=cutoff,
    )
    assert len(rows) == 1
    assert rows[0].observed_itm is None
    assert kwargs_seen["issued_before"] == cutoff
    assert kwargs_seen["label_as_of"] == cutoff
