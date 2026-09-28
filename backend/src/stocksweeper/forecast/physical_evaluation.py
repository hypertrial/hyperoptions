"""Paired, date-blocked evaluation of as-issued physical forecast challengers."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import log
from statistics import mean
from typing import Literal

import numpy as np

from stocksweeper.forecast.calendar import SessionCalendar

Band = Literal["1", "2-5", "6-25"]
_BANDS: dict[Band, tuple[int, int]] = {"1": (1, 1), "2-5": (2, 5), "6-25": (6, 25)}
_BASELINE = "lognormal_ewma"
_PREDECLARED_PHYSICAL_COMPARISONS = 10


@dataclass(frozen=True)
class ContestRow:
    ticker: str
    origin: date
    expiry_session: date
    horizon: int
    strike: str
    side: Literal["call", "put"]
    method: str
    probability: float | None
    observed_itm: bool | None
    provenance: Literal["as_issued", "immutable_replay"]
    moneyness: str = "unknown"
    volatility_regime: str = "unknown"
    event_status: str = "unknown"
    reason: str | None = None
    input_vintage: str | None = None
    issued_at: datetime | None = None
    issuance_key: str | None = None
    contract_id: str | None = None
    crps: float | None = None
    prepare_ms: float | None = None
    lookup_ms: float | None = None


def crps(prices: tuple[float, ...], weights: tuple[float, ...], observed: float) -> float:
    """Exact weighted empirical CRPS, O(n log n) for already-sorted prices."""
    if len(prices) != len(weights) or not prices or not np.isfinite(observed):
        raise ValueError("finite observation and matching nonempty scenarios required")
    if any(not np.isfinite(price) or price <= 0 for price in prices):
        raise ValueError("scenario prices must be finite and positive")
    if any(not np.isfinite(weight) or weight < 0 for weight in weights):
        raise ValueError("scenario weights must be finite and nonnegative")
    if abs(sum(weights) - 1) > 1e-8:
        raise ValueError("scenario weights must sum to one")
    pairs = sorted(zip(prices, weights, strict=True))
    first = sum(weight * abs(price - observed) for price, weight in pairs)
    cumulative_weight = cumulative_price = second = 0.0
    for price, weight in pairs:
        second += weight * (price * cumulative_weight - cumulative_price)
        cumulative_weight += weight
        cumulative_price += weight * price
    return first - second


def _score(probability: float, observed: bool) -> tuple[float, float]:
    clipped = min(max(probability, 1e-6), 1 - 1e-6)
    label = float(observed)
    return (probability - label) ** 2, -(label * log(clipped) + (1 - label) * log(1 - clipped))


def _quantile(values: list[float], q: float) -> float | None:
    return float(np.quantile(values, q)) if values else None


def _blocked_dates(origins: list[date], band: Band, calendar: SessionCalendar) -> list[date]:
    chosen: list[date] = []
    next_allowed: date | None = None
    for origin in sorted(set(origins)):
        if next_allowed is None or origin >= next_allowed:
            chosen.append(origin)
            next_allowed = calendar.offset(origin, _BANDS[band][1] + 1)
    return chosen


def _issued_order(row: ContestRow) -> tuple[datetime, str]:
    """A fixed, pre-outcome choice for repeated same-session issuances."""
    return row.issued_at or datetime.max.replace(tzinfo=UTC), row.issuance_key or ""


def evaluate_band(
    rows: list[ContestRow],
    candidate: str,
    band: Band,
    *,
    holdout_start: date | None = None,
    period: Literal["screen", "holdout", "all"] = "all",
    calendar: SessionCalendar | None = None,
    bootstrap_samples: int = 2000,
) -> dict[str, object]:
    """Compare identical contracts and decide eligibility; never change live routing.

    `screen` excludes any outcome reaching the untouched final period. `holdout`
    uses origins from that period. Calendar dates, with all tickers together,
    are the resampling and independence blocks.
    """
    if candidate == _BASELINE or candidate == "":
        raise ValueError("candidate must differ from baseline")
    if period != "all" and holdout_start is None:
        raise ValueError("period split requires holdout_start")
    if bootstrap_samples < 100:
        raise ValueError("at least 100 bootstrap samples required")
    calendar = calendar or SessionCalendar()
    lower, upper = _BANDS[band]
    relevant = [
        row
        for row in rows
        if lower <= row.horizon <= upper
        and row.method in (_BASELINE, candidate)
        and (
            period == "all"
            or (period == "screen" and row.expiry_session < holdout_start)
            or (period == "holdout" and row.origin >= holdout_start)
        )
    ]
    dates = _blocked_dates([row.origin for row in relevant], band, calendar)
    chosen_dates = set(dates)
    relevant = [row for row in relevant if row.origin in chosen_dates]
    first_baseline: dict[tuple[str, date, int], ContestRow] = {}
    for row in relevant:
        if row.method != _BASELINE:
            continue
        unit = (row.ticker, row.origin, row.horizon)
        previous = first_baseline.get(unit)
        if previous is None or _issued_order(row) < _issued_order(previous):
            first_baseline[unit] = row
    selected_vintages = {unit: row.input_vintage for unit, row in first_baseline.items()}
    orphan_attempts = [
        row for row in relevant
        if row.method == candidate
        and (row.ticker, row.origin, row.horizon) not in selected_vintages
    ]
    orphan_contracts = {
        (row.ticker, row.origin, row.expiry_session, row.contract_id or f"{row.strike}|{row.side}")
        for row in orphan_attempts
    }
    original_attempts = len(relevant)
    relevant = [
        row
        for row in relevant
        if (row.ticker, row.origin, row.horizon) in selected_vintages
        and row.input_vintage == selected_vintages[(row.ticker, row.origin, row.horizon)]
        and (row.method == _BASELINE or row.input_vintage is not None)
    ]
    revised_attempts_excluded = original_attempts - len(relevant) - len(orphan_attempts)
    by_contract: dict[tuple[str, date, date, str], dict[str, ContestRow]] = defaultdict(dict)
    duplicate_attempts_excluded = 0
    for row in relevant:
        key = (
            row.ticker,
            row.origin,
            row.expiry_session,
            row.contract_id or f"{row.strike}|{row.side}",
        )
        prior = by_contract[key].get(row.method)
        if prior is not None:
            duplicate_attempts_excluded += 1
        if prior is None or _issued_order(row) < _issued_order(prior):
            by_contract[key][row.method] = row
    rejection_reasons: Counter[str] = Counter()
    rejection_reasons["baseline_not_issued"] += len(orphan_contracts)
    unit_pairs: dict[tuple[str, date, int], list[tuple[ContestRow, ContestRow]]] = defaultdict(list)
    latencies: dict[str, list[float]] = defaultdict(list)
    timed_prepare: set[tuple[str, date]] = set()
    timed_lookup: set[tuple[str, date, int]] = set()
    baseline_attempted = baseline_available = candidate_available = 0
    for pair in by_contract.values():
        baseline = pair.get(_BASELINE)
        challenger = pair.get(candidate)
        if challenger is not None:
            prepare_key = (challenger.ticker, challenger.origin)
            lookup_key = (challenger.ticker, challenger.origin, challenger.horizon)
            if prepare_key not in timed_prepare:
                timed_prepare.add(prepare_key)
                value = challenger.prepare_ms
                if value is not None and np.isfinite(value) and value >= 0:
                    latencies["prepare"].append(value)
            if lookup_key not in timed_lookup:
                timed_lookup.add(lookup_key)
                value = challenger.lookup_ms
                if value is not None and np.isfinite(value) and value >= 0:
                    latencies["lookup"].append(value)
        if baseline is None:
            rejection_reasons["baseline_not_issued"] += 1
            continue
        baseline_attempted += 1
        if baseline.probability is None:
            rejection_reasons[baseline.reason or "baseline_unavailable"] += 1
            continue
        baseline_available += 1
        if challenger is None or challenger.probability is None:
            rejection_reasons[
                "input_vintage_missing"
                if baseline.input_vintage is None
                else "candidate_not_issued"
                if challenger is None
                else challenger.reason or "unavailable"
            ] += 1
            continue
        candidate_available += 1
        if (
            baseline.observed_itm is None
            or challenger.observed_itm is None
            or baseline.observed_itm != challenger.observed_itm
            or baseline.provenance != challenger.provenance
        ):
            rejection_reasons["unscorable_or_conflicting_label"] += 1
            continue
        if not 0 <= baseline.probability <= 1 or not 0 <= challenger.probability <= 1:
            rejection_reasons["invalid_probability"] += 1
            continue
        unit_pairs[(baseline.ticker, baseline.origin, baseline.horizon)].append(
            (baseline, challenger)
        )
    units: list[dict] = []
    subgroup_values: dict[tuple[str, str, str, date, int], list[float]] = defaultdict(list)
    calibration_bins: dict[str, list[list[tuple[float, float]]]] = {
        side: [[] for _ in range(10)] for side in ("call", "put")
    }
    for (ticker, origin, horizon), pairs in unit_pairs.items():
        baseline_scores = [_score(base.probability, base.observed_itm) for base, _ in pairs]
        candidate_scores = [
            _score(challenger.probability, challenger.observed_itm) for _, challenger in pairs
        ]
        brier_delta = mean(
            b[0] - a[0] for a, b in zip(baseline_scores, candidate_scores, strict=True)
        )
        log_delta = mean(
            b[1] - a[1] for a, b in zip(baseline_scores, candidate_scores, strict=True)
        )
        crps_values = [
            (base.crps, challenger.crps)
            for base, challenger in pairs
            if base.crps is not None and challenger.crps is not None
        ]
        units.append(
            {
                "ticker": ticker,
                "origin": origin,
                "horizon": horizon,
                "brier_baseline": mean(item[0] for item in baseline_scores),
                "brier_candidate": mean(item[0] for item in candidate_scores),
                "log_baseline": mean(item[1] for item in baseline_scores),
                "log_candidate": mean(item[1] for item in candidate_scores),
                "brier_delta": brier_delta,
                "log_delta": log_delta,
                "crps_baseline": mean(a for a, _ in crps_values) if crps_values else None,
                "crps_candidate": mean(b for _, b in crps_values) if crps_values else None,
            }
        )
        unit_calibration: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)
        for base, challenger in pairs:
            unit_calibration[
                challenger.side, min(9, int(challenger.probability * 10))
            ].append((challenger.probability, float(challenger.observed_itm)))
            for category, value in (
                ("horizon", str(horizon)),
                ("moneyness", challenger.moneyness),
                ("volatility_regime", challenger.volatility_regime),
                ("event_status", challenger.event_status),
            ):
                base_brier = _score(base.probability, base.observed_itm)[0]
                challenger_brier = _score(challenger.probability, challenger.observed_itm)[0]
                subgroup_values[(category, value, ticker, origin, horizon)].append(
                    challenger_brier - base_brier
                )
        for (side, bin_index), values in unit_calibration.items():
            calibration_bins[side][bin_index].append(
                (mean(p for p, _ in values), mean(y for _, y in values))
            )
    by_date: dict[date, list[dict]] = defaultdict(list)
    for unit in units:
        by_date[unit["origin"]].append(unit)
    date_blocks = sorted(by_date)
    brier_ci: tuple[float, float] | None = None
    log_ci: tuple[float, float] | None = None
    brier_familywise_ci: tuple[float, float] | None = None
    if date_blocks:
        rng = np.random.default_rng(20260927)
        sampled = rng.integers(0, len(date_blocks), size=(bootstrap_samples, len(date_blocks)))
        block_counts = np.asarray([len(by_date[day]) for day in date_blocks])
        brier_sums = np.asarray(
            [sum(unit["brier_delta"] for unit in by_date[day]) for day in date_blocks]
        )
        log_sums = np.asarray(
            [sum(unit["log_delta"] for unit in by_date[day]) for day in date_blocks]
        )
        sampled_counts = block_counts[sampled].sum(axis=1)
        brier_boot = brier_sums[sampled].sum(axis=1) / sampled_counts
        log_boot = log_sums[sampled].sum(axis=1) / sampled_counts
        brier_ci = (_quantile(brier_boot.tolist(), 0.025), _quantile(brier_boot.tolist(), 0.975))
        log_ci = (_quantile(log_boot.tolist(), 0.025), _quantile(log_boot.tolist(), 0.975))
        tail = 0.05 / (2 * _PREDECLARED_PHYSICAL_COMPARISONS)
        brier_familywise_ci = (
            _quantile(brier_boot.tolist(), tail),
            _quantile(brier_boot.tolist(), 1 - tail),
        )
    subgroup_units: dict[tuple[str, str], list[tuple[date, float]]] = defaultdict(list)
    for (category, value, _, origin, _), deltas in subgroup_values.items():
        subgroup_units[(category, value)].append((origin, mean(deltas)))
    subgroups = {
        f"{category}:{value}": {
            "ticker_origin_horizon_units": len(values),
            "date_blocks": len({day for day, _ in values}),
            "brier_delta": mean(delta for _, delta in values),
            "supported": len(values) >= 100 and len({day for day, _ in values}) >= 10,
        }
        for (category, value), values in sorted(subgroup_units.items())
    }
    origins = {unit["origin"] for unit in units}
    tickers = {unit["ticker"] for unit in units}
    provenance = {row.provenance for row in relevant}
    brier_delta = mean(unit["brier_delta"] for unit in units) if units else None
    log_delta = mean(unit["log_delta"] for unit in units) if units else None
    def calibration(bins: list[list[tuple[float, float]]]) -> list[dict[str, float | int | None]]:
        return [
            {
                "lower": index / 10,
                "upper": (index + 1) / 10,
                "count": len(values),
                "forecast_mean": mean(p for p, _ in values) if values else None,
                "observed_rate": mean(y for _, y in values) if values else None,
            }
            for index, values in enumerate(bins)
        ]

    calibration_by_side = {side: calibration(bins) for side, bins in calibration_bins.items()}
    crps_units = [unit for unit in units if unit["crps_candidate"] is not None]
    return {
        "candidate": candidate,
        "band": band,
        "period": period,
        "holdout_start": holdout_start.isoformat() if holdout_start else None,
        "provenance": sorted(provenance),
        "later_vintage_attempts_excluded": revised_attempts_excluded,
        "duplicate_attempts_excluded": duplicate_attempts_excluded,
        "contract_cells_attempted": len(by_contract) + len(orphan_contracts),
        "baseline_contract_forecasts_attempted": baseline_attempted,
        "contract_forecasts_available": candidate_available,
        "baseline_contract_forecasts_available": baseline_available,
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "tickers": len(tickers),
        "independent_date_blocks": len(origins),
        "ticker_origin_horizon_units": len(units),
        "brier": {
            "baseline": mean(item["brier_baseline"] for item in units) if units else None,
            "candidate": mean(item["brier_candidate"] for item in units) if units else None,
            "paired_delta": brier_delta,
            "bootstrap_95": brier_ci,
            "bootstrap_familywise_95": brier_familywise_ci,
            "comparison_count": _PREDECLARED_PHYSICAL_COMPARISONS,
        },
        "log_loss": {
            "baseline": mean(item["log_baseline"] for item in units) if units else None,
            "candidate": mean(item["log_candidate"] for item in units) if units else None,
            "paired_delta": log_delta,
            "bootstrap_95": log_ci,
        },
        "crps": {
            "scored_units": len(crps_units),
            "baseline": mean(item["crps_baseline"] for item in crps_units) if crps_units else None,
            "candidate": mean(item["crps_candidate"] for item in crps_units)
            if crps_units
            else None,
        },
        "calibration_by_side": calibration_by_side,
        "subgroups": subgroups,
        "latency_ms": {
            label: {"p50": _quantile(values, 0.5), "p95": _quantile(values, 0.95)}
            for label, values in latencies.items()
        },
    }
