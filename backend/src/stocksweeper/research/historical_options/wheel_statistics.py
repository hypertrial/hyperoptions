"""Pure, descriptive wheel metrics and calendar-paired exploratory inference.

One input row is one scored origin for one rule, including intentional no-trade
outcomes. Contract counts never provide additional independent date support.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


def tail_stats(values: Sequence[float], weights: Sequence[float] | None = None,
               alpha: float = .05) -> dict:
    """Return the weighted left quantile and fractional-mass lower-tail mean.

Weights are nonnegative mass, not independent observations. Only the required
fraction of a boundary atom enters expected shortfall; its sign is retained.
The field names refer to the default 5% tail even when ``alpha`` is supplied.
"""
    if not math.isfinite(alpha) or not 0 < alpha <= 1:
        raise ValueError("tail mass must be finite and in (0, 1]")
    data = np.asarray(values, dtype=float)
    mass = np.ones(len(data)) if weights is None else np.asarray(weights, dtype=float)
    if data.ndim != 1 or mass.ndim != 1 or data.shape != mass.shape:
        raise ValueError("values and weights must be equally sized one-dimensional arrays")
    if not np.isfinite(data).all() or not np.isfinite(mass).all() or (mass < 0).any():
        raise ValueError("tail values and weights must be finite with nonnegative weights")
    if not len(data):
        return {"quantile05": None, "expected_shortfall05": None, "tail_loss": None}
    if not (mass > 0).any():
        raise ValueError("positive tail weight required")
    # Scaling preserves proportions while preventing overflow for large finite weights.
    mass = mass / mass.max()
    order = np.argsort(data, kind="stable")
    data, mass = data[order], mass[order]
    positive = mass > 0
    data, mass = data[positive], mass[positive]
    cumulative = np.cumsum(mass)
    target = alpha * math.fsum(mass)
    index = min(int(np.searchsorted(cumulative, target, side="left")), len(data) - 1)
    consumed = np.minimum(mass, np.maximum(target - np.r_[0., cumulative[:-1]], 0.))
    shortfall = math.fsum(data * (consumed / target))
    if not math.isfinite(shortfall):
        raise ValueError("nonfinite expected shortfall")
    return {"quantile05": float(data[index]), "expected_shortfall05": shortfall,
            "tail_loss": max(0., -shortfall)}


def _validated_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    for row in rows:
        origin = row["origin"]
        if origin in seen:
            raise ValueError("duplicate scored origin; deduplicate shared outcomes first")
        seen.add(origin)
        days = row["assessment_days"]
        if isinstance(days, bool) or not isinstance(days, (int, np.integer)) or days <= 0:
            raise ValueError("positive integer assessment days required")
        try:
            valid = all(math.isfinite(float(row[name]))
                        for name in ("return", "baseline_return"))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("finite economic and baseline returns required")
        if not isinstance(row.get("traded", True), (bool, np.bool_)):
            raise ValueError("traded must be a boolean")
        row["expiry_session"]  # Require maturity provenance even for a no-trade outcome.
    return sorted(rows, key=lambda row: row["origin"])


def summarize(rows: list[dict]) -> dict:
    """Summarize independent origins using a ratio of sums for reward per day.

Intentional no-trade observations remain scored: callers supply stock retention
for a call state and cash retention for a put state as both return and benchmark.
"""
    rows = _validated_rows(rows)
    count = len(rows)
    result = {"scored": count, "origin_dates": count,
              "expiries": len({row["expiry_session"] for row in rows}),
              "traded": sum(bool(row.get("traded", True)) for row in rows),
              "traded_expiries": len({row["expiry_session"] for row in rows
                                      if row.get("traded", True)}),
              "total_assessment_days": sum(row["assessment_days"] for row in rows),
              "mean_return": None, "return_per_day": None, "median_return": None,
              "loss_frequency": None, "mean_baseline_return": None,
              "excess_return_per_day": None,
              **tail_stats([row["return"] for row in rows])}
    if not count:
        return result
    values = [float(row["return"]) for row in rows]
    baselines = [float(row["baseline_return"]) for row in rows]
    days = result["total_assessment_days"]
    result.update(mean_return=math.fsum(value / count for value in values),
                  return_per_day=math.fsum(value / days for value in values),
                  median_return=float(np.median(values)),
                  loss_frequency=sum(value < 0 for value in values) / count,
                  mean_baseline_return=math.fsum(value / count for value in baselines),
                  excess_return_per_day=math.fsum(
                      value / days - baseline / days
                      for value, baseline in zip(values, baselines, strict=True)))
    if any(not math.isfinite(result[name]) for name in (
        "mean_return", "return_per_day", "median_return", "mean_baseline_return",
        "excess_return_per_day",
    )):
        raise ValueError("nonfinite aggregate metric")
    return result


def _bootstrap_metrics(rows: list[dict], weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray([row["return"] for row in rows], dtype=float)
    days = np.asarray([row["assessment_days"] for row in rows], dtype=float)
    reward = (weights @ values) / (weights @ days)
    order = np.argsort(values, kind="stable")
    mass = weights[:, order]
    cumulative = np.cumsum(mass, axis=1)
    target = .05 * mass.sum(axis=1)
    consumed = np.minimum(mass, np.maximum(
        target[:, None] - np.c_[np.zeros(len(weights)), cumulative[:, :-1]], 0.))
    shortfall = (consumed @ values[order]) / target
    return reward, shortfall


def moving_block_interval(rows: list[dict], calendar_dates: Sequence,
                          paired_rows: list[dict] | None = None,
                          draws: int = 2000, seed: int = 1729,
                          block_length: int = 26) -> dict:
    """Resample complete moving calendar windows with paired date missingness.

Every rule/ticker must receive the same global calendar and seed. Sampled calendar
indices depend only on that schedule, never on contract availability. Support is
the number of full nonoverlapping calendar blocks containing at least one scored
origin (a matched origin for comparisons); all-missing blocks provide no support.
The final incomplete calendar block is descriptive only for support counting.
"""
    for value, name in ((draws, "draws"), (block_length, "block length")):
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 1:
            raise ValueError(f"positive integer {name} required")
    calendar = list(calendar_dates)
    if calendar != sorted(set(calendar)):
        raise ValueError("calendar dates must be unique and increasing")
    rows = _validated_rows(rows)
    positions = {origin: index for index, origin in enumerate(calendar)}
    if any(row["origin"] not in positions for row in rows):
        raise ValueError("scored origin outside supplied calendar")
    comparator = None
    if paired_rows is not None:
        comparator = _validated_rows(paired_rows)
        if any(row["origin"] not in positions for row in comparator):
            raise ValueError("paired origin outside supplied calendar")
        by_date = {row["origin"]: row for row in comparator}
        rows = [row for row in rows if row["origin"] in by_date]
        comparator = [by_date[row["origin"]] for row in rows]
    support = {positions[row["origin"]] for row in rows}
    support_counts = [sum(index in support for index in range(start, start + block_length))
                      for start in range(0, len(calendar) - block_length + 1, block_length)]
    supported_blocks = sum(count > 0 for count in support_counts)
    result = {"status": "underpowered", "origin_dates": len(rows),
              "complete_blocks": supported_blocks,
              "calendar_complete_blocks": len(support_counts),
              "supported_dates_per_block": support_counts,
              "interval_return_per_day": None, "interval_expected_shortfall05": None,
              "bootstrap_draws": draws, "bootstrap_valid_draws": 0,
              "block_length": block_length, "seed": seed,
              "multiplicity": "exploratory"}
    if comparator is not None:
        left, right = summarize(rows), summarize(comparator)
        result.update(paired_origin_dates=len(rows), delta_return_per_day=None,
                      delta_expected_shortfall05=None, interval_delta_return_per_day=None,
                      interval_delta_expected_shortfall05=None)
        if rows:
            result.update(delta_return_per_day=left["return_per_day"]-right["return_per_day"],
                          delta_expected_shortfall05=(left["expected_shortfall05"]
                                                     - right["expected_shortfall05"]))
    if len(rows) < 20 or supported_blocks < 8:
        return result
    rng = np.random.default_rng(seed)
    count = len(calendar)
    starts = rng.integers(0, count - block_length + 1,
                         size=(draws, math.ceil(count / block_length)))
    sampled = (starts[:, :, None] + np.arange(block_length)).reshape(draws, -1)[:, :count]
    calendar_weights = np.zeros((draws, count), dtype=np.int32)
    np.add.at(calendar_weights, (np.arange(draws)[:, None], sampled), 1)
    weights = calendar_weights[:, [positions[row["origin"]] for row in rows]]
    valid = weights.sum(axis=1) > 0
    result["bootstrap_valid_draws"] = int(valid.sum())
    # An empty scored draw is evidence of sparse support, not an outcome of zero.
    if not valid.all():
        result["reason"] = "calendar bootstrap contains draws without scored support"
        return result
    reward, shortfall = _bootstrap_metrics(rows, weights)
    if not np.isfinite(reward).all() or not np.isfinite(shortfall).all():
        raise ValueError("nonfinite bootstrap metric")
    result.update(status="estimable",
                  interval_return_per_day=np.quantile(reward, [.025, .975]).tolist(),
                  interval_expected_shortfall05=np.quantile(shortfall, [.025, .975]).tolist())
    if comparator is not None:
        other_reward, other_shortfall = _bootstrap_metrics(comparator, weights)
        if not np.isfinite(other_reward).all() or not np.isfinite(other_shortfall).all():
            raise ValueError("nonfinite paired bootstrap metric")
        result.update(interval_delta_return_per_day=np.quantile(
            reward-other_reward, [.025, .975]).tolist(),
            interval_delta_expected_shortfall05=np.quantile(
                shortfall-other_shortfall, [.025, .975]).tolist())
    return result


def pareto_representatives(stats: list[dict]) -> dict:
    """Choose supported positive-reward frontier rules without a utility weight.

Reward per assessment day is maximized and nonnegative tail loss minimized.
Equal points prefer lower loss frequency, then greater origin coverage, then
lexical rule ID. Balanced is the lower middle of the reward-ordered frontier.
"""
    result = dict.fromkeys(("conservative", "balanced", "higher_return"))
    eligible = [row for row in stats
                if row["origin_dates"] >= 20 and row["expiries"] >= 4
                and row.get("traded", row["origin_dates"]) >= 20
                and row.get("traded_expiries", row["expiries"]) >= 4
                and row["return_per_day"] is not None and row["tail_loss"] is not None
                and row["loss_frequency"] is not None
                and all(math.isfinite(row[key]) for key in (
                    "return_per_day", "tail_loss", "loss_frequency"))
                and row["return_per_day"] > 0 and row["tail_loss"] >= 0]
    def tie(row: dict) -> tuple:
        return row["loss_frequency"], -row["origin_dates"], row["rule_id"]
    points = {}
    for row in sorted(eligible, key=tie):
        points.setdefault((row["return_per_day"], row["tail_loss"]), row)
    frontier = [row for row in points.values() if not any(
        other["return_per_day"] >= row["return_per_day"]
        and other["tail_loss"] <= row["tail_loss"]
        and (other["return_per_day"] > row["return_per_day"]
             or other["tail_loss"] < row["tail_loss"])
        for other in points.values())]
    frontier.sort(key=lambda row: (row["return_per_day"], *tie(row)))
    if frontier:
        result.update(conservative=min(frontier, key=lambda row: (
            row["tail_loss"], *tie(row)))["rule_id"],
            balanced=frontier[(len(frontier)-1)//2]["rule_id"],
            higher_return=frontier[-1]["rule_id"])
    return result
