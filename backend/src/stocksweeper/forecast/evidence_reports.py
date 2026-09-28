"""Dated, descriptive model evidence for the local comparison UI.

These reports describe matched accuracy; they never select a live model.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from options_api.market_calendar import session_close
from stocksweeper.forecast.capture_windows import capture_window_counts
from options_api.intraday_capture import _COMPARATOR_VERSION
from options_api.intraday_shadow import _VERSION as INTRADAY_VERSION
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.intraday_evidence import evaluate as evaluate_intraday
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.forecast.physical_contest import (
    EMPIRICAL_SHADOW_VERSION,
    GJR_VERSION,
    STUDENT_VERSION,
    HAR_VERSION,
    SKEW_T_VERSION,
    EGARCH_VERSION,
    MARKOV_VERSION,
    NGBOOST_VERSION,
)
from stocksweeper.forecast.physical_evaluation import ContestRow, evaluate_band
from stocksweeper.forecast.predictive import BASELINE_VERSION

CANDIDATE_VERSIONS = {
    "empirical_scaled": EMPIRICAL_SHADOW_VERSION,
    "student_t_ewma": STUDENT_VERSION,
    "gjr_garch_t": GJR_VERSION,
    "ohlc_har": HAR_VERSION,
    "skew_t_ewma": SKEW_T_VERSION,
    "egarch_skew_t": EGARCH_VERSION,
    "markov_switching": MARKOV_VERSION,
    "ngboost_pooled": NGBOOST_VERSION,
    "earnings_jump": "earnings-jump-v1",
    "iv_physical": "iv-physical-v1",
}
BANDS = {"1": range(1, 2), "2-5": range(2, 6), "6-25": range(6, 26)}


def ledger_band_rows(
    ledger: ForecastLedger,
    calendar: SessionCalendar,
    provenance: str,
    candidate: str,
    holdout_start: date | None,
    period: str,
    band: str,
    as_of: datetime | None = None,
    since: date | None = None,
    current_version_only: bool = False,
) -> list[ContestRow]:
    """Adapt first-party issuance rows to the paired evaluator's exact grain."""
    horizons = BANDS[band]
    start = max(
        (day for day in (since, holdout_start if period == "holdout" else None) if day),
        default=None,
    )
    issues = ledger.iter_evaluation_rows(
        provenance=provenance,
        since=start,
        expiry_before=holdout_start if period == "screen" else None,
        methods=("lognormal_ewma", candidate),
        horizon_range=(horizons.start, horizons.stop - 1),
        with_crps=True,
        issued_before=as_of,
        label_as_of=as_of,
    )
    rows: list[ContestRow] = []
    candidate_keys: set[tuple[object, ...]] = set()
    for item in issues:
        origin = item["input_session"]
        method = item["method"]
        if origin is None or method is None:
            continue
        expected_version = (
            BASELINE_VERSION if method == "lognormal_ewma"
            else CANDIDATE_VERSIONS.get(candidate)
        )
        if current_version_only and item.get("model_version") != expected_version:
            continue
        label_valid = (
            item["label_status"] == "valid"
            and item["label_checked_at"] is not None
            and item["label_checked_at"] >= session_close(item["expiry_session"])
            and item["label_checked_at"] > item["issued_at"]
            and item["issued_at"] < session_close(item["expiry_session"])
        )
        spot = Decimal(item["spot_exact"]) if item["spot_exact"] else None
        relative = abs(float(Decimal(item["strike_exact"]) / spot - 1)) if spot else None
        moneyness = (
            "unknown" if relative is None else
            "near_atm" if relative <= 0.05 else
            "moderate" if relative <= 0.15 else "tail"
        )
        row = ContestRow(
            ticker=item["ticker"], origin=origin,
            expiry_session=item["expiry_session"],
            horizon=calendar.horizon(origin, item["expiration"]),
            strike=item["strike_exact"], side=item["side"], method=method,
            probability=(item["itm_probability"] if item["status"] == "available" else None),
            observed_itm=(bool(item["observed_itm"]) if label_valid else None),
            provenance=provenance, moneyness=moneyness,
            volatility_regime=item["volatility_regime"] or "unknown",
            event_status=item["known_event_status"] or "unknown",
            reason=item["unavailable_reason"] or item["label_reason"],
            input_vintage=item["data_hash"], issued_at=item["issued_at"],
            issuance_key=item["idempotency_key"], contract_id=item["contract_key"],
            crps=(item["crps"] if label_valid else None),
            prepare_ms=item["prepare_ms"], lookup_ms=item["lookup_ms"],
        )
        if current_version_only and candidate != "lognormal_ewma":
            key = (row.ticker, row.origin, row.expiry_session, row.contract_id)
            if method == candidate:
                candidate_keys.add(key)
        rows.append(row)
    if current_version_only and candidate != "lognormal_ewma":
        return [
            row for row in rows
            if row.method == candidate
            or (row.ticker, row.origin, row.expiry_session, row.contract_id)
            in candidate_keys
        ]
    return rows


def ledger_contest(
    ledger: ForecastLedger,
    calendar: SessionCalendar,
    provenance: str,
    candidate: str,
    holdout_start: date | None,
    period: str,
    as_of: datetime | None = None,
    since: date | None = None,
    current_version_only: bool = False,
) -> dict[str, Any]:
    start = max(
        (day for day in (since, holdout_start if period == "holdout" else None) if day),
        default=None,
    )
    return {
        "source": "append-only forecast ledger",
        "provenance": provenance,
        "skipped_attempts": ledger.evaluation_skipped_attempts(provenance),
        "prospective_panel_coverage": (
            ledger.panel_coverage(
                since=start,
                before=holdout_start if period == "screen" else None,
                as_of=as_of,
            ) if provenance == "as_issued" else None
        ),
        "bands": {
            band: evaluate_band(
                ledger_band_rows(
                    ledger, calendar, provenance, candidate, holdout_start, period, band,
                    as_of=as_of,
                    since=since,
                    current_version_only=current_version_only,
                ),
                candidate, band, holdout_start=holdout_start, period=period, calendar=calendar,
            )
            for band in BANDS
        },
    }


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def save_replay_report(data_dir: Path, candidate: str, report: dict[str, Any]) -> Path:
    """Store explicitly retrospective evidence outside Git, atomically."""
    if candidate not in CANDIDATE_VERSIONS or report.get("provenance") != "immutable_replay":
        raise ValueError("invalid replay candidate or provenance")
    path = data_dir / "forecast" / "evidence" / f"replay-{candidate}.json"
    payload = {
        "schema_version": 2,
        "candidate": candidate,
        "candidate_version": CANDIDATE_VERSIONS[candidate],
        "baseline_version": BASELINE_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "report": report,
        "report_hash": _digest(report),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".replay-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def load_replay_report(data_dir: Path, candidate: str) -> dict[str, Any] | None:
    if candidate not in CANDIDATE_VERSIONS:
        return None
    path = data_dir / "forecast" / "evidence" / f"replay-{candidate}.json"
    try:
        payload = json.loads(path.read_text())
        report = payload["report"]
        if (
            payload.get("schema_version") != 2
            or payload["candidate"] != candidate
            or payload["candidate_version"] != CANDIDATE_VERSIONS[candidate]
            or payload["baseline_version"] != BASELINE_VERSION
            or report["provenance"] != "immutable_replay"
            or payload["report_hash"] != _digest(report)
            or not isinstance(report["bands"], dict)
        ):
            return None
        return payload
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _summary(
    band_report: dict[str, Any], *, provenance: str, generated_at: str,
    report_hash: str, model_version: str, reference_only: bool = False,
    audit_session: str | None = None, audit_frozen_at: str | None = None,
    evidence_window_start: date | None = None,
    coverage_basis: str = "recorded_contract_cells_with_baseline_issuance",
) -> dict[str, Any]:
    dates = band_report["independent_date_blocks"]
    brier = dict(band_report["brier"])
    log_loss = dict(band_report["log_loss"])
    if dates < 20:
        brier["bootstrap_95"] = None
        brier["bootstrap_familywise_95"] = None
        log_loss["bootstrap_95"] = None
    if reference_only:
        brier["paired_delta"] = None
        brier["bootstrap_95"] = None
        brier["bootstrap_familywise_95"] = None
        log_loss["paired_delta"] = None
        log_loss["bootstrap_95"] = None
    return {
        "provenance": provenance,
        "generated_at": generated_at,
        "report_hash": report_hash,
        "model_version": model_version,
        "input_version": (
            "frozen-yahoo-snapshot-v1" if provenance == "immutable_replay"
            else "forecast-ledger-v1"
        ),
        "audit_session": audit_session,
        "audit_frozen_at": audit_frozen_at,
        "evidence_window_start": (
            evidence_window_start.isoformat() if evidence_window_start else None
        ),
        "tickers": band_report["tickers"],
        "independent_date_blocks": dates,
        "ticker_origin_horizon_units": band_report["ticker_origin_horizon_units"],
        "contract_forecasts_available": band_report["contract_forecasts_available"],
        "contract_cells_attempted": band_report["contract_cells_attempted"],
        "coverage": (
            band_report["contract_forecasts_available"] / band_report["contract_cells_attempted"]
            if band_report["contract_cells_attempted"] else None
        ),
        "coverage_basis": coverage_basis,
        "replay_scheduled_units": band_report.get("replay_scheduled_units"),
        "replay_baseline_available_units": band_report.get("replay_baseline_available_units"),
        "replay_fit_coverage": (
            band_report["replay_baseline_available_units"] / band_report["replay_scheduled_units"]
            if band_report.get("replay_scheduled_units") else None
        ),
        "replay_rejection_reasons": band_report.get("replay_rejection_reasons"),
        "rejection_reasons": band_report["rejection_reasons"],
        "brier": brier,
        "log_loss": log_loss,
        "crps": band_report["crps"],
        "calibration_by_side": band_report["calibration_by_side"],
        "calibration_count_basis": "independent_ticker_origin_horizon_side_bin",
        "subgroups": band_report["subgroups"],
        "latency_ms": band_report["latency_ms"],
        "significance": (
            "not_applicable" if reference_only else
            "exploratory_interval" if dates >= 20 else "not_estimable"
        ),
    }


def build_model_evidence(
    data_dir: Path, ledger: ForecastLedger, as_of: datetime,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build once in a background task; HTTP lookups read the resulting map."""
    calendar = SessionCalendar()
    # ponytail: cap the daily ledger scan at three years; use offline grouped reports if it grows.
    since = as_of.date() - timedelta(days=1096)
    capture_counts = capture_window_counts(data_dir, as_of - timedelta(days=35), as_of)
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for band in BANDS:
        baseline_rows = ledger_band_rows(
            ledger, calendar, "as_issued", "lognormal_ewma", None, "all", band,
            as_of=as_of, since=since, current_version_only=True,
        )
        reference = evaluate_band(
            [*baseline_rows, *(replace(row, method="baseline_reference") for row in baseline_rows)],
            "baseline_reference", band, calendar=calendar, bootstrap_samples=100,
        )
        result[("lognormal_ewma", band)] = {
            "prospective": _summary(
                reference, provenance="as_issued", generated_at=as_of.isoformat(),
                report_hash=_digest(reference), model_version=BASELINE_VERSION,
                reference_only=True,
                evidence_window_start=since,
                coverage_basis="recorded_current_version_baseline_attempts",
            ),
            "retrospective": None,
        }
    for candidate, version in CANDIDATE_VERSIONS.items():
        prospective = ledger_contest(
            ledger, calendar, "as_issued", candidate, None, "all", as_of=as_of,
            since=since, current_version_only=True,
        )
        replay = load_replay_report(data_dir, candidate)
        prospective_hash = _digest(prospective)
        for band in BANDS:
            result[(candidate, band)] = {
                "prospective": _summary(
                    prospective["bands"][band], provenance="as_issued",
                    generated_at=as_of.isoformat(), report_hash=prospective_hash,
                    model_version=version,
                    evidence_window_start=since,
                    coverage_basis="recorded_current_version_candidate_cells",
                ),
                "retrospective": (
                    _summary(
                        replay["report"]["bands"][band], provenance="immutable_replay",
                        generated_at=replay["generated_at"], report_hash=replay["report_hash"],
                        model_version=version,
                        audit_session=replay["report"].get("audit_session"),
                        audit_frozen_at=replay["report"].get("audit_frozen_at"),
                    ) if replay is not None and band in replay["report"]["bands"] else None
                ),
            }
    for band, horizons in BANDS.items():
        intraday_versions = {
            "lognormal_ewma": BASELINE_VERSION,
            "quote_reanchored_comparator": _COMPARATOR_VERSION,
            "intraday_shadow": INTRADAY_VERSION,
        }
        intraday_rows = [row for row in ledger.iter_evaluation_rows(
            provenance="as_issued",
            methods=("lognormal_ewma", "quote_reanchored_comparator", "intraday_shadow"),
            horizon_range=(horizons.start, horizons.stop - 1),
            since=since,
            issued_before=as_of,
            label_as_of=as_of,
        ) if row.get("model_version") == intraday_versions.get(row.get("method"))]
        report = evaluate_intraday(intraday_rows, as_of=as_of)
        summary = report["overall"]
        blocks = summary["scored_date_blocks"]
        intervals = summary["paired_calendar_date_bootstrap_95"] if blocks >= 20 else None
        result[("intraday_shadow", band)] = {
            "prospective": {
                "provenance": "as_issued_matched_intraday_windows",
                "generated_at": as_of.isoformat(),
                "report_hash": _digest(report),
                "model_version": INTRADAY_VERSION,
                "input_version": "forecast-ledger-quote-snapshot-v1",
                "evidence_window_start": since.isoformat(),
                "tickers": summary["scored_tickers"],
                "independent_date_blocks": blocks,
                "ticker_origin_horizon_units": summary[
                    "scored_ticker_date_window_expiry_units"
                ],
                "contract_forecasts_available": summary[
                    "forecast_available_cells"
                ]["intraday_shadow"],
                "contract_cells_attempted": summary["recorded_contract_windows"],
                "coverage": (
                    summary["forecast_available_cells"]["intraday_shadow"]
                    / summary["recorded_contract_windows"]
                    if summary["recorded_contract_windows"] else None
                ),
                "coverage_basis": "recorded_intraday_contract_windows",
                "coverage_limit": report["denominator_limit"],
                "capture_windows_all_horizons": capture_counts,
                "rejection_reasons": summary["rejection_reasons"],
                "brier": {
                    "baseline": summary["mean"]["brier"]["dated_close"],
                    "candidate": summary["mean"]["brier"]["intraday_shadow"],
                    "paired_delta": summary[
                        "paired_shadow_minus_reference"
                    ]["brier"]["dated_close"],
                    "bootstrap_95": intervals["brier"]["dated_close"] if intervals else None,
                },
                "log_loss": {
                    "baseline": summary["mean"]["log_loss"]["dated_close"],
                    "candidate": summary["mean"]["log_loss"]["intraday_shadow"],
                    "paired_delta": summary[
                        "paired_shadow_minus_reference"
                    ]["log_loss"]["dated_close"],
                    "bootstrap_95": intervals["log_loss"]["dated_close"] if intervals else None,
                },
                "quote_reanchored_comparator": {
                    metric: summary["mean"][metric]["quote_reanchored_comparator"]
                    for metric in ("brier", "log_loss")
                },
                "calibration_by_side": summary["calibration_by_side"],
                "calibration_count_basis": "independent_ticker_date_window_expiry_side_bin",
                "latency_ms": summary["latency_ms"].get("intraday_shadow", {}),
                "by_window": report["by_window"],
                "by_expiry": report["by_expiry"],
                "by_calendar": report["by_calendar"],
                "significance": "exploratory_interval" if intervals else "not_estimable",
            },
            "retrospective": None,
        }
    return result
