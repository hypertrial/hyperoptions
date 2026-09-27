"""Summarize unpublished constrained-curve quote fit, coverage, and latency."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from statistics import median

import numpy as np

from stocksweeper.config import load_settings
from stocksweeper.storage.db import connect


def _latency(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": median(values) if values else None,
        "p95": float(np.quantile(values, 0.95)) if values else None,
    }


def report() -> dict[str, object]:
    path = load_settings().resolved_data_dir() / "results.duckdb"
    with connect(path) as connection:
        records = connection.execute(
            """SELECT report_json FROM market_curve_shadow_runs
               WHERE model_version = 'constrained-call-curve-shadow-v2'
               ORDER BY chain_fetched_at"""
        ).fetchall()
    held: Counter[str] = Counter()
    cohorts: Counter[str] = Counter()
    comparable_snapshots = 0
    coverage: dict[str, Counter[str]] = defaultdict(Counter)
    rejections: Counter[str] = Counter()
    latencies: dict[str, list[float]] = defaultdict(list)
    for (raw,) in records:
        row = json.loads(raw) if isinstance(raw, str) else raw
        cohorts[row.get("held_out_cohort", "unknown")] += 1
        comparable_snapshots += bool(row.get("held_out_comparison_ready"))
        for metric in (
            "held_out_inside",
            "held_out_count",
            "shadow_held_out_predicted",
            "benchmark_held_out_inside",
            "benchmark_held_out_predicted",
            "paired_held_out_count",
            "paired_shadow_inside",
            "paired_benchmark_inside",
        ):
            held[metric] += int(row.get(metric, 0))
        rejections.update(row.get("rejection_reasons", {}))
        for metric in ("elapsed_ms", "benchmark_ms", "live_refresh_ms"):
            value = row.get(metric)
            if isinstance(value, (float, int)) and value >= 0:
                latencies[metric].append(float(value))
        for expiry, bands in row.get("by_expiry_moneyness", {}).items():
            for moneyness, counts in bands.items():
                for grouping in (f"expiry:{expiry}", f"moneyness:{moneyness}"):
                    coverage[grouping].update(counts)
    return {
        "kind": "research_only_quote_consistency",
        "snapshots": len(records),
        "held_out_cohorts": dict(sorted(cohorts.items())),
        "comparable_snapshots": comparable_snapshots,
        "replacement_review_eligible": bool(records) and comparable_snapshots == len(records),
        "held_out_quote_interval_fit": {
            "held_out": held["held_out_count"],
            "shadow": {
                "predicted": held["shadow_held_out_predicted"],
                "inside": held["held_out_inside"],
                "coverage": (
                    held["shadow_held_out_predicted"] / held["held_out_count"]
                    if held["held_out_count"]
                    else None
                ),
                "inside_fraction": (
                    held["held_out_inside"] / held["shadow_held_out_predicted"]
                    if held["shadow_held_out_predicted"]
                    else None
                ),
            },
            "benchmark": {
                "predicted": held["benchmark_held_out_predicted"],
                "inside": held["benchmark_held_out_inside"],
                "coverage": (
                    held["benchmark_held_out_predicted"] / held["held_out_count"]
                    if held["held_out_count"]
                    else None
                ),
                "inside_fraction": (
                    held["benchmark_held_out_inside"] / held["benchmark_held_out_predicted"]
                    if held["benchmark_held_out_predicted"]
                    else None
                ),
            },
            "paired": {
                "predicted": held["paired_held_out_count"],
                "shadow_inside": held["paired_shadow_inside"],
                "benchmark_inside": held["paired_benchmark_inside"],
                "shadow_inside_fraction": (
                    held["paired_shadow_inside"] / held["paired_held_out_count"]
                    if held["paired_held_out_count"]
                    else None
                ),
                "benchmark_inside_fraction": (
                    held["paired_benchmark_inside"] / held["paired_held_out_count"]
                    if held["paired_held_out_count"]
                    else None
                ),
            },
        },
        "coverage": {
            key: {
                **dict(value),
                "benchmark_fraction": value["benchmark_available"] / value["contracts"],
                "shadow_fraction": value["shadow_available"] / value["contracts"],
            }
            for key, value in sorted(coverage.items())
        },
        "rejection_reasons": dict(sorted(rejections.items())),
        "latency_ms": {key: _latency(values) for key, values in sorted(latencies.items())},
    }


if __name__ == "__main__":
    print(json.dumps(report(), indent=2, sort_keys=True))
