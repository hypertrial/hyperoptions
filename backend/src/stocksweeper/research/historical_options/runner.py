"""Offline orchestration for three bounded, independently auditable studies."""
from __future__ import annotations

import json
import platform
import resource
import sys
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import polars as pl

from stocksweeper.research.historical_options.artifacts import (
    atomic_directory, finalize_manifest, safe_path,
)
from stocksweeper.research.historical_options.reports import write_report, write_summary
from stocksweeper.research.historical_options.statistics import (
    calibration_bins, forecast_descriptives, forecast_inference, loss_calibration_bins,
    strategy_inference,
)

SETTINGS = {
    "version": 1, "horizons": [1, 25], "bootstrap_draws": 2000, "bootstrap_seed": 1729,
    "minimum_paired_dates": 20, "band_spacing": {"1": 2, "2-5": 6, "6-25": 26},
    "holdout_session_fraction": .25, "fit_latency_enforced": False,
    "premium_haircuts": [0, .05, .10, .20], "option_fees": [0, .65],
    "call_stock_slippage_bps": [0, 10, 25], "cash_yield": 0,
    "research_vintage": "current-vintage retrospective", "numerical_threads": 1,
}


def environment_versions() -> dict:
    packages = ("duckdb", "polars", "pyarrow", "numpy", "scipy", "arch", "statsmodels",
                "exchange-calendars", "regimelib")
    from stocksweeper.forecast import physical_contest as physical
    from stocksweeper.forecast.predictive import BASELINE_VERSION

    models = {"lognormal_ewma": BASELINE_VERSION}
    models.update({name: getattr(physical, constant) for name, constant in (
        ("empirical_scaled", "EMPIRICAL_SHADOW_VERSION"),
        ("student_t_ewma", "STUDENT_VERSION"), ("gjr_garch_t", "GJR_VERSION"),
        ("ohlc_har", "HAR_VERSION"), ("skew_t_ewma", "SKEW_T_VERSION"),
        ("egarch_skew_t", "EGARCH_VERSION"), ("markov_switching", "MARKOV_VERSION"),
    )})
    return {"python": platform.python_version(), "model_versions": models,
            "dependencies": {name: version(name) for name in packages},
            "polars_threads": pl.thread_pool_size()}


def peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def run_snapshot(snapshot: Path, output: Path) -> dict:
    # Imports stay inside the command so freeze does not initialize model dependencies.
    from stocksweeper.research.historical_options.calibration import run_calibration
    from stocksweeper.research.historical_options.liquidity import run_liquidity
    from stocksweeper.research.historical_options.snapshot import (
        environment_metadata, verify_snapshot,
    )
    from stocksweeper.research.historical_options.strategies import run_strategies

    started = perf_counter()
    snapshot = safe_path(snapshot, must_exist=True)
    output = safe_path(output)
    if output == snapshot or output.is_relative_to(snapshot) or snapshot.is_relative_to(output):
        raise ValueError("run output and frozen input paths must not overlap")
    manifest = verify_snapshot(snapshot)
    with atomic_directory(output) as stage:
        days = pl.read_parquet(snapshot / "days.parquet", columns=[
            "ticker", "contract", "session", "expiry", "expiry_session", "side", "strike",
            "mark", "volume", "transactions", "regular_hours", "reconciliation_failed",
            "valid", "reason", "opening_open", "opening_start", "opening_end",
        ])
        stocks = pl.read_parquet(snapshot / "stocks.parquet")
        # Hours are used only for activity/timing, never as forecast training data.
        print("Running calibration on frozen observations", flush=True)
        calibration = run_calibration(days, stocks, stage)
        print("Running predetermined selections and fixed stresses", flush=True)
        hours = pl.read_parquet(snapshot / "hours.parquet", columns=[
            "ticker", "contract", "session", "hour", "start", "end", "open", "volume",
            "transactions", "regular_session", "valid", "reason", "expiry", "side", "strike",
        ])
        strategies = run_strategies(days, stocks, hours, stage)
        print("Auditing complete archive liquidity", flush=True)
        liquidity = run_liquidity(days, stocks, hours, stage)
        del hours, days, stocks
        print("Computing paired inference and reports", flush=True)
        summary = {
            "version": 1, "snapshot_hash": manifest["canonical_hash"], "settings": SETTINGS,
            "environment": environment_versions(),
            "coverage": {key: manifest[key] for key in
                         ("source_counts", "stock_metadata", "normalization_counts",
                          "exclusion_counts", "fetch_audit", "daily_validation_sources")
                         if key in manifest},
            "studies": {"calibration": calibration, "strategies": strategies,
                        "liquidity": liquidity},
            "forecast_inference": forecast_inference(stage / "forecast_cells.parquet"),
            "strategy_inference": strategy_inference(stage / "outcomes.parquet"),
            "calibration_bins": calibration_bins(stage / "forecast_cells.parquet"),
            "forecast_descriptives": forecast_descriptives(stage / "forecast_cells.parquet"),
            "loss_calibration_bins": loss_calibration_bins(stage / "forecast_cells.parquet"),
        }
        write_summary(stage, summary)
        write_report(stage, summary)
        runtime = {"duration_seconds": perf_counter()-started, "peak_rss_bytes": peak_rss_bytes(),
                   "memory_target_bytes": 4 * 1024**3}
        (stage / "resource.json").write_text(json.dumps(runtime, sort_keys=True, indent=2)+"\n")
        completed = finalize_manifest(stage, {
            "version": 1, "kind": "historical-options-run",
            "snapshot_hash": manifest["canonical_hash"], "settings": SETTINGS,
            "environment": environment_versions(), "runtime": runtime,
            "code": environment_metadata(),
            "snapshot_environment": manifest.get("environment", {}),
        })
    return completed
