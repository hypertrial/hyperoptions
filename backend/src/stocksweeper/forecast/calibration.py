"""Causal peer-return calibration and ticker-disjoint holdout audit.

The inputs are matured, volatility-standardized returns. This module never reads
prices or selects rules; it only audits one signal state and session horizon.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.models import State

_FIT_START = date(2021, 1, 1)
_FIT_END = date(2022, 12, 31)
_AUDIT_START = date(2023, 1, 1)
_AUDIT_END = date(2025, 12, 31)
_BRIER_GRID = np.array([0.01, *np.arange(0.05, 1.0, 0.05), 0.99])
_BOOTSTRAPS = 512


@dataclass(frozen=True)
class Observation:
    ticker: str
    as_of: date
    maturity: date
    horizon: int
    state: State
    standardized_return: float


@dataclass(frozen=True)
class CalibrationResult:
    fit_values: tuple[float, ...]
    baseline_values: tuple[float, ...]
    fit_peers: int
    audit_peers: int
    audit_blocks: int
    crps_skill_lower_90: float | None
    brier_delta: float | None
    support_low: float | None
    support_high: float | None
    qualified: bool
    reason: str
    fit_samples: int = 0
    audit_samples: int = 0


def tail_probability(
    sorted_returns: Sequence[float], threshold: float, side: Literal["call", "put"]
) -> float:
    """Empirical strict ITM tail; equality belongs to neither side."""
    if not sorted_returns or not np.isfinite(threshold):
        raise ValueError("a nonempty distribution and finite threshold are required")
    if side == "call":
        return (len(sorted_returns) - bisect_right(sorted_returns, threshold)) / len(sorted_returns)
    if side == "put":
        return bisect_left(sorted_returns, threshold) / len(sorted_returns)
    raise ValueError("side must be call or put")


def fit_audit(
    fit: Sequence[Observation],
    audit: Sequence[Observation],
    horizon: int,
    state: State,
    seed: int = 7,
) -> CalibrationResult:
    """Fit on 2021–22 and audit on unseen tickers in 2023–25.

    A failed gate returns a reason and never marks the distribution qualified.
    Malformed labels or overlapping tickers raise instead of silently training
    on future information.
    """
    if horizon < 1 or state not in {"long", "flat"}:
        raise ValueError("invalid horizon or signal state")
    calendar = SessionCalendar()
    _validate(fit, horizon, _FIT_START, _FIT_END, calendar)
    _validate(audit, horizon, _AUDIT_START, _AUDIT_END, calendar)
    if {item.ticker for item in fit} & {item.ticker for item in audit}:
        raise ValueError("fit and audit tickers must be disjoint")

    fit_state = [item for item in fit if item.state == state]
    audit_state, block_ids = _independent_audit(
        [item for item in audit if item.state == state], horizon, calendar
    )
    values = tuple(sorted(item.standardized_return for item in fit_state))
    baseline = tuple(sorted(item.standardized_return for item in fit))
    fit_peers = len({item.ticker for item in fit_state})
    audit_peers = len({item.ticker for item in audit_state})
    support_low, support_high = _support(audit_state)
    blocks = len(set(block_ids))

    def result(
        reason: str,
        *,
        lower: float | None = None,
        brier: float | None = None,
    ) -> CalibrationResult:
        return CalibrationResult(
            fit_values=values,
            baseline_values=baseline,
            fit_peers=fit_peers,
            audit_peers=audit_peers,
            audit_blocks=blocks,
            crps_skill_lower_90=lower,
            brier_delta=brier,
            support_low=support_low,
            support_high=support_high,
            qualified=reason == "passed",
            reason=reason,
            fit_samples=len(fit_state),
            audit_samples=len(audit_state),
        )

    if fit_peers < 50:
        return result("insufficient_fit_peers")
    if audit_peers < 15:
        return result("insufficient_audit_peers")
    if blocks < 30:
        return result("insufficient_audit_blocks")

    outcomes = np.asarray([item.standardized_return for item in audit_state], dtype=np.float64)
    conditional_crps = _empirical_crps(values, outcomes)
    baseline_crps = _empirical_crps(baseline, outcomes)
    lower = _skill_lower_bound(conditional_crps, baseline_crps, block_ids, seed)
    brier = _strike_grid_brier_delta(values, baseline, outcomes)
    if lower <= 0:
        return result("crps_audit_failed", lower=lower, brier=brier)
    if brier > 1e-12:
        return result("brier_audit_failed", lower=lower, brier=brier)
    return result("passed", lower=lower, brier=brier)


def _validate(
    observations: Sequence[Observation],
    horizon: int,
    start: date,
    end: date,
    calendar: SessionCalendar,
) -> None:
    seen: set[tuple[str, date, int]] = set()
    maturity_by_as_of: dict[date, date] = {}
    for item in observations:
        if not item.ticker or item.horizon != horizon or item.state not in {"long", "flat"}:
            raise ValueError("invalid observation identity")
        if not np.isfinite(item.standardized_return):
            raise ValueError("nonfinite standardized return")
        if not (start <= item.as_of < item.maturity <= end):
            raise ValueError("observation leaks outside its fit or audit window")
        expected = maturity_by_as_of.get(item.as_of)
        if expected is None:
            expected = calendar.offset(item.as_of, horizon)
            maturity_by_as_of[item.as_of] = expected
        if item.maturity != expected:
            raise ValueError("maturity is not the requested scheduled-session horizon")
        key = (item.ticker, item.as_of, item.horizon)
        if key in seen:
            raise ValueError("duplicate ticker-session-horizon observation")
        seen.add(key)


def _support(observations: Sequence[Observation]) -> tuple[float | None, float | None]:
    if not observations:
        return None, None
    values = np.asarray([item.standardized_return for item in observations])
    low, high = np.quantile(values, [0.01, 0.99])
    return float(low), float(high)


def _independent_audit(
    observations: Sequence[Observation], horizon: int, calendar: SessionCalendar
) -> tuple[list[Observation], tuple[int, ...]]:
    """Embargo labels whose return interval crosses a calendar block.

    A block spans at least one complete forecast horizon. Adjacent blocks may
    share an endpoint close, but their return intervals never overlap. Fixed
    audit-start anchoring gives the same blocks to both signal states.
    """
    if not observations:
        return [], ()
    first = calendar.sessions(_AUDIT_START, _AUDIT_END)[0]
    last = max(item.maturity for item in observations)
    ordinal = {day: index for index, day in enumerate(calendar.sessions(first, last))}
    width = max(5, horizon)
    retained: list[Observation] = []
    blocks: list[int] = []
    for item in observations:
        block = ordinal[item.as_of] // width
        if ordinal[item.maturity] <= (block + 1) * width:
            retained.append(item)
            blocks.append(block)
    return retained, tuple(blocks)


def _empirical_crps(sorted_sample: Sequence[float], outcomes: np.ndarray) -> np.ndarray:
    """Exact empirical CRPS without allocating a sample-by-audit matrix."""
    sample = np.asarray(sorted_sample, dtype=np.float64)
    size = len(sample)
    if size == 0:
        raise ValueError("empty calibration distribution")
    cumulative = np.concatenate(([0.0], np.cumsum(sample)))
    split = np.searchsorted(sample, outcomes, side="right")
    left = outcomes * split - cumulative[split]
    right = cumulative[-1] - cumulative[split] - outcomes * (size - split)
    pair_sum = np.dot(2 * np.arange(size) - size + 1, sample)
    return (left + right) / size - pair_sum / (size * size)


def _skill_lower_bound(
    conditional_crps: np.ndarray,
    baseline_crps: np.ndarray,
    blocks: Sequence[int],
    seed: int,
) -> float:
    labels = np.asarray(blocks, dtype=np.int64)
    cond_sums = np.bincount(labels, weights=conditional_crps)
    base_sums = np.bincount(labels, weights=baseline_crps)
    present = np.bincount(labels) > 0
    cond_sums = cond_sums[present]
    base_sums = base_sums[present]
    sampled = np.random.default_rng(seed).integers(
        0, len(cond_sums), size=(_BOOTSTRAPS, len(cond_sums))
    )
    base = base_sums[sampled].sum(axis=1)
    cond = cond_sums[sampled].sum(axis=1)
    skills = np.full(len(base), -np.inf)
    valid = base > 0
    skills[valid] = 1.0 - cond[valid] / base[valid]
    return float(np.quantile(skills, 0.10))


def _strike_grid_brier_delta(
    conditional: Sequence[float], baseline: Sequence[float], outcomes: np.ndarray
) -> float:
    thresholds = np.unique(np.quantile(baseline, _BRIER_GRID))
    deltas: list[float] = []
    for threshold in thresholds:
        for side in ("call", "put"):
            conditioned = tail_probability(conditional, float(threshold), side)
            unconditioned = tail_probability(baseline, float(threshold), side)
            observed = outcomes > threshold if side == "call" else outcomes < threshold
            deltas.append(
                float(np.mean((conditioned - observed) ** 2 - (unconditioned - observed) ** 2))
            )
    return max(deltas)
