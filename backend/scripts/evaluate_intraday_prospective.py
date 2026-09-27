"""Score recorded, as-issued intraday snapshots against exact matured closes.

Run from backend/: uv run --group research python scripts/evaluate_intraday_prospective.py
Only recorded watchlist snapshots form the denominator; this report never
promotes a model or calls retrospective current-vintage history as-issued.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np

from options_api.market_calendar import _calendar, session_close
from stocksweeper.config import load_settings
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.forecast.physical_evaluation import _quantile, _score

_NY = ZoneInfo("America/New_York")
_WINDOWS = ("10:00", "13:00", "15:30")
_MODELS = ("dated_close", "quote_reanchored_comparator", "intraday_shadow")
_CHALLENGERS = _MODELS[1:]


def _cell_key(row: dict[str, object]) -> tuple[object, ...]:
    return (
        row["contract_key"],
        row["root"],
        row["side"],
        row["expiration"],
        row["expiry_session"],
        row["strike_exact"],
        row["terms_note"],
        row["contract_since"],
        row["issued_at"].astimezone(_NY).date(),
        row["snapshot_window"],
    )


def _vintage(row: dict[str, object]) -> tuple[object, ...]:
    # A refetch can change retrieval time without changing the model input.
    return row["input_session"], row["data_hash"]


def _order(row: dict[str, object]) -> tuple[datetime, str]:
    return row["issued_at"], row["idempotency_key"]


def _calendar_stratum(day: date) -> str:
    calendar = _calendar()
    if not calendar.is_session(day.isoformat()):
        return "holiday_or_non_session"
    session = calendar.date_to_session(day.isoformat(), direction="none")
    duration = calendar.session_close(session) - calendar.session_open(session)
    return "early_close" if duration < timedelta(hours=6, minutes=30) else "regular_session"


def _summary(cells: list[dict[str, object]], *, bootstrap: bool = False) -> dict[str, object]:
    available: Counter[str] = Counter()
    issued: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    latencies: dict[str, list[float]] = defaultdict(list)
    quote_ages: list[float] = []
    target_lags: list[float] = []
    units: dict[tuple[object, ...], list[dict[str, tuple[float, float]]]] = defaultdict(list)
    for cell in cells:
        attempts = cell["attempts"]
        for model, attempt in attempts.items():
            if attempt is None:
                continue
            issued[model] += 1
            if attempt["status"] == "available":
                available[model] += 1
            latency = attempt["lookup_ms"]
            if (
                model in _CHALLENGERS
                and latency is not None
                and np.isfinite(latency)
                and latency >= 0
            ):
                latencies[model].append(float(latency))
        reason = cell["reason"]
        if reason is not None:
            reasons[reason] += 1
        if cell["scores"] is not None:
            units[(cell["ticker"], cell["day"], cell["window"], cell["expiry_session"])].append(
                cell["scores"]
            )
        if cell["quote_age_ms"] is not None:
            quote_ages.append(cell["quote_age_ms"])
        if cell["target_lag_ms"] is not None:
            target_lags.append(cell["target_lag_ms"])
    unit_scores = [
        {
            model: (
                mean(contract[model][0] for contract in contracts),
                mean(contract[model][1] for contract in contracts),
            )
            for model in _MODELS
        }
        for contracts in units.values()
    ]
    means = {
        metric: {
            model: mean(score[model][index] for score in unit_scores) if unit_scores else None
            for model in _MODELS
        }
        for index, metric in enumerate(("brier", "log_loss"))
    }
    deltas = {
        metric: {
            reference: (
                means[metric]["intraday_shadow"] - means[metric][reference] if unit_scores else None
            )
            for reference in _MODELS[:2]
        }
        for metric in ("brier", "log_loss")
    }
    intervals = None
    if bootstrap and len({key[1] for key in units}) >= 2:
        by_day: dict[date, list[dict[str, tuple[float, float]]]] = defaultdict(list)
        for key, contracts in units.items():
            by_day[key[1]].append(
                {
                    model: (
                        mean(contract[model][0] for contract in contracts),
                        mean(contract[model][1] for contract in contracts),
                    )
                    for model in _MODELS
                }
            )
        days = sorted(by_day)
        counts = np.asarray([len(by_day[day]) for day in days])
        sampled = np.random.default_rng(20260927).integers(0, len(days), size=(2000, len(days)))
        sampled_counts = counts[sampled].sum(axis=1)
        intervals = {}
        for index, metric in enumerate(("brier", "log_loss")):
            intervals[metric] = {}
            for reference in _MODELS[:2]:
                sums = np.asarray(
                    [
                        sum(
                            score["intraday_shadow"][index] - score[reference][index]
                            for score in by_day[day]
                        )
                        for day in days
                    ]
                )
                values = sums[sampled].sum(axis=1) / sampled_counts
                intervals[metric][reference] = [
                    _quantile(values.tolist(), 0.025),
                    _quantile(values.tolist(), 0.975),
                ]
    return {
        "recorded_contract_windows": len(cells),
        "recorded_contracts": len({cell["contract_key"] for cell in cells}),
        "recorded_tickers": len({cell["ticker"] for cell in cells}),
        "recorded_dates": len({cell["day"] for cell in cells}),
        "forecast_present_cells": {model: issued[model] for model in _MODELS},
        "forecast_available_cells": {model: available[model] for model in _MODELS},
        "triple_available_contract_windows": sum(
            all(
                attempt is not None and attempt["status"] == "available"
                for attempt in cell["attempts"].values()
            )
            for cell in cells
        ),
        "scored_contract_windows": sum(cell["scores"] is not None for cell in cells),
        "scored_ticker_date_window_expiry_units": len(units),
        "scored_tickers": len({key[0] for key in units}),
        "scored_date_blocks": len({key[1] for key in units}),
        "mean": means,
        "paired_shadow_minus_reference": deltas,
        "paired_calendar_date_bootstrap_95": intervals,
        "rejection_reasons": dict(sorted(reasons.items())),
        "latency_ms": {
            model: {
                "p50": _quantile(latencies[model], 0.5),
                "p95": _quantile(latencies[model], 0.95),
            }
            for model in _CHALLENGERS
        },
        "quote_age_ms": {"p50": _quantile(quote_ages, 0.5), "p95": _quantile(quote_ages, 0.95)},
        "target_to_issue_lag_ms": {
            "p50": _quantile(target_lags, 0.5),
            "p95": _quantile(target_lags, 0.95),
        },
    }


def evaluate(rows: list[dict[str, object]], *, as_of: datetime | None = None) -> dict[str, object]:
    """Use first prospective attempt per contract/window; never select by outcome."""
    now = as_of or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    primary: dict[tuple[object, ...], list[dict[str, object]]] = defaultdict(list)
    candidates: dict[tuple[object, ...], dict[str, list[dict[str, object]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        if row["provenance"] != "as_issued":
            continue
        if row["method"] in _CHALLENGERS and row["snapshot_window"] in _WINDOWS:
            candidates[_cell_key(row)][row["method"]].append(row)
        elif row["price_basis"] == "completed_close" and row["snapshot_window"] is None:
            key = _cell_key({**row, "snapshot_window": None})[:8]
            primary[key].append(row)
    cells: list[dict[str, object]] = []
    duplicate_attempts = 0
    for key, attempts in sorted(candidates.items(), key=lambda pair: tuple(map(str, pair[0]))):
        contract = key[:8]
        day, window = key[-2:]
        target = datetime.combine(day, time.fromisoformat(window), _NY).astimezone(UTC)
        selected = {
            model: min(attempts[model], key=_order) if attempts[model] else None
            for model in _CHALLENGERS
        }
        duplicate_attempts += sum(max(0, len(attempts[model]) - 1) for model in _CHALLENGERS)
        first = min((row for row in selected.values() if row is not None), key=_order)
        last = max((row for row in selected.values() if row is not None), key=_order)
        # Primary issues are idempotent across unchanged data; a window refresh
        # may retain the earlier as-issued close forecast rather than insert one.
        same_contract = [row for row in primary[contract] if row["issued_at"] <= first["issued_at"]]
        same_vintage = [row for row in same_contract if _vintage(row) == _vintage(first)]
        baseline = max(same_vintage, key=_order) if same_vintage else None
        trio = {"dated_close": baseline, **selected}
        reason = None
        if len(selected) != 2 or any(row is None for row in selected.values()):
            reason = "missing_challenger_attempt"
        elif selected[_CHALLENGERS[0]]["issued_at"] != selected[_CHALLENGERS[1]]["issued_at"]:
            reason = "challenger_capture_time_mismatch"
        elif _vintage(selected[_CHALLENGERS[0]]) != _vintage(selected[_CHALLENGERS[1]]):
            reason = "challenger_input_vintage_mismatch"
        elif baseline is None:
            reason = "baseline_input_vintage_mismatch" if same_contract else "baseline_not_issued"
        elif any(row["status"] != "available" for row in trio.values()):
            reason = next(
                row["unavailable_reason"] or f"{model}_unavailable"
                for model, row in trio.items()
                if row["status"] != "available"
            )
        elif any(
            row["itm_probability"] is None or not 0 <= row["itm_probability"] <= 1
            for row in trio.values()
        ):
            reason = "invalid_probability"
        elif (
            selected[_CHALLENGERS[0]]["quote_digest"] is None
            or selected[_CHALLENGERS[0]]["quote_digest"]
            != selected[_CHALLENGERS[1]]["quote_digest"]
        ):
            reason = "quote_vintage_missing_or_mismatch"
        elif any(row["price_basis"] != "underlying_quote" for row in selected.values()):
            reason = "quote_price_basis_mismatch"
        elif (
            _calendar_stratum(day) == "holiday_or_non_session"
            or target >= session_close(day)
            or any(
                not target <= row["issued_at"] < target + timedelta(minutes=5)
                for row in selected.values()
            )
        ):
            reason = "snapshot_outside_regular_session"
        elif any(row["label_status"] != "valid" for row in trio.values()):
            reason = next(
                row["label_reason"] or row["label_status"] or "label_missing"
                for row in trio.values()
                if row["label_status"] != "valid"
            )
        elif (
            None in {row["observed_itm"] for row in trio.values()}
            or len({row["observed_itm"] for row in trio.values()}) != 1
        ):
            reason = "conflicting_exact_labels"
        elif any(row["issued_at"] >= session_close(row["expiry_session"]) for row in trio.values()):
            reason = "issued_after_expiry_close"
        elif (
            first["expiry_session"] != last["expiry_session"]
            or session_close(first["expiry_session"]) > now
            or any(
                row["label_checked_at"] is None
                or row["label_checked_at"] < session_close(row["expiry_session"])
                or row["label_checked_at"] <= row["issued_at"]
                or row["label_checked_at"] > now
                for row in trio.values()
            )
        ):
            reason = "label_not_mature_at_scoring"
        scores = None
        if reason is None:
            observed = bool(first["observed_itm"])
            scores = {
                model: _score(float(row["itm_probability"]), observed)
                for model, row in trio.items()
            }
        quote_time = first["quote_time"]
        cells.append(
            {
                "contract_key": first["contract_key"],
                "ticker": first["ticker"],
                "day": day,
                "window": window,
                "expiry_session": first["expiry_session"],
                "calendar": _calendar_stratum(day),
                "expiry": "same_day" if first["expiry_session"] == day else "future",
                "attempts": trio,
                "reason": reason,
                "scores": scores,
                "quote_age_ms": (
                    (first["issued_at"] - quote_time).total_seconds() * 1000
                    if quote_time is not None and first["issued_at"] >= quote_time
                    else None
                ),
                "target_lag_ms": (
                    (first["issued_at"] - target).total_seconds() * 1000
                    if first["issued_at"] >= target
                    else None
                ),
            }
        )
    return {
        "source": "append-only forecast ledger",
        "provenance": "as_issued",
        "scope": "recorded watchlist intraday contract windows only",
        "denominator_limit": "Missed windows are absent from the issuance ledger.",
        "metric": "binary ITM at exact expiry-session close; equality is ATM",
        "selection": "First challenger attempt, preceding matching close forecast",
        "duplicate_challenger_attempts_excluded": duplicate_attempts,
        "overall": _summary(cells, bootstrap=True),
        "by_window": {
            window: _summary([cell for cell in cells if cell["window"] == window])
            for window in _WINDOWS
        },
        "by_expiry": {
            expiry: _summary([cell for cell in cells if cell["expiry"] == expiry])
            for expiry in ("same_day", "future")
        },
        "by_calendar": {
            calendar: _summary([cell for cell in cells if cell["calendar"] == calendar])
            for calendar in ("regular_session", "early_close", "holiday_or_non_session")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=load_settings().resolved_data_dir())
    parser.add_argument(
        "--since", type=date.fromisoformat, help="earliest completed-close input session"
    )
    args = parser.parse_args()
    rows = ForecastLedger(args.data_dir).evaluation_rows(provenance="as_issued", since=args.since)
    print(json.dumps(evaluate(rows), sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
