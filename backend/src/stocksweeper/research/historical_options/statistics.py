"""Predeclared paired inference, separate from all-session descriptive research."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from stocksweeper.forecast.calendar import SessionCalendar

BASELINE = "lognormal_ewma"
BANDS = {"1": (1, 1, 2), "2-5": (2, 5, 6), "6-25": (6, 25, 26)}
PANELS = {
    "primary": (date(2024, 10, 29), date(2025, 12, 31)),
    "supplement": (date(2026, 1, 1), date(2026, 9, 28)),
}
METRICS = ("brier", "log_loss", "crps", "normalized_crps", "pinball")


def inference_schedule(start: date, end: date, spacing: int,
                       calendar: SessionCalendar | None = None) -> tuple[date, ...]:
    """Calendar-fixed before observing availability, common to every ticker/model."""
    if spacing < 1:
        raise ValueError("positive session spacing required")
    return (calendar or SessionCalendar()).sessions(start, end)[::spacing]


def holdout_boundary(start: date, end: date,
                     calendar: SessionCalendar | None = None) -> date:
    sessions = (calendar or SessionCalendar()).sessions(start, end)
    if not sessions:
        raise ValueError("panel has no calendar sessions")
    return sessions[int(len(sessions) * .75)]


def paired_interval(units: pl.DataFrame, *, adjusted: bool = False,
                    draws: int = 2000, seed: int = 1729) -> dict:
    """Resample entire paired calendar dates, preserving all their ticker units."""
    if draws < 1:
        raise ValueError("positive bootstrap draws required")
    clusters = units.group_by("origin").agg(
        pl.col("delta").sum().alias("sum"), pl.len().alias("count")
    ).sort("origin")
    result = {"paired_dates": clusters.height, "paired_units": units.height,
              "paired_delta": units["delta"].mean() if units.height else None,
              "status": "not_estimable", "interval": None,
              "comparison_count": 7 if adjusted else 1,
              "multiplicity": "seven_comparison_familywise" if adjusted else "exploratory"}
    if clusters.height < 20:
        return result
    rng = np.random.default_rng(seed)
    samples = rng.integers(0, clusters.height, size=(draws, clusters.height))
    sums = clusters["sum"].to_numpy()
    counts = clusters["count"].to_numpy()
    values = sums[samples].sum(axis=1) / counts[samples].sum(axis=1)
    tail = .05 / (2 * (7 if adjusted else 1))
    result.update(status="estimable", interval=np.quantile(values, [tail, 1-tail]).tolist())
    return result


def forecast_inference(path: Path) -> list[dict]:
    """Match identical contract cells first, then average shared maturity outcomes."""
    source = pl.scan_parquet(path)
    columns = source.collect_schema().names()
    keys = ["panel", "ticker", "origin", "expiry_session", "horizon", "contract"]
    metrics = [m for m in METRICS if m in columns]
    source = source.filter(pl.col("primary_eligible")).select(*keys, "model", *metrics)
    # Duplicated policy selections or repeated records cannot add independent support.
    source = source.unique(subset=[*keys, "model"], keep="first")
    baseline = source.filter(pl.col("model") == BASELINE).drop("model").rename(
        {m: f"baseline_{m}" for m in metrics}
    )
    pairs = source.filter(pl.col("model") != BASELINE).join(baseline, on=keys, how="inner")
    expressions = []
    for metric in metrics:
        valid = pl.col(metric).is_finite() & pl.col(f"baseline_{metric}").is_finite()
        expressions.extend([
            pl.col(metric).filter(valid).mean().alias(f"candidate_{metric}"),
            pl.col(f"baseline_{metric}").filter(valid).mean().alias(f"baseline_{metric}"),
            valid.sum().alias(f"cells_{metric}"),
        ])
    # One bounded aggregation before iterating comparisons avoids repeated archive joins.
    aggregated = pairs.group_by("panel", "model", "ticker", "origin", "horizon").agg(
        pl.col("expiry_session").first(), *expressions
    ).collect(engine="streaming")
    calendar = SessionCalendar()
    models = source.select("model").unique().collect()["model"].to_list()
    results = []
    for panel, (start, end) in PANELS.items():
        boundary = holdout_boundary(start, end, calendar)
        for band, (low, high, spacing) in BANDS.items():
            schedule = inference_schedule(start, end, spacing, calendar)
            for period in ("development", "holdout"):
                eligible = aggregated.filter(
                    (pl.col("panel") == panel) & pl.col("horizon").is_between(low, high)
                    & pl.col("origin").is_in(schedule)
                )
                if period == "development":
                    eligible = eligible.filter(pl.col("expiry_session") < boundary)
                else:
                    eligible = eligible.filter(pl.col("origin") >= boundary)
                for metric in metrics:
                    frame = eligible.filter(pl.col(f"cells_{metric}") > 0).select(
                        "model", "ticker", "origin", "horizon",
                        pl.col(f"candidate_{metric}").alias("candidate"),
                        pl.col(f"baseline_{metric}").alias("baseline"),
                        pl.col(f"cells_{metric}").alias("matched_contract_cells"),
                    ).with_columns((pl.col("candidate")-pl.col("baseline")).alias("delta"))
                    for model in sorted(m for m in models if m != BASELINE):
                        units = frame.filter(pl.col("model") == model)
                        adjusted = panel == "primary" and period == "holdout" and metric == "brier"
                        result = paired_interval(units, adjusted=adjusted)
                        result.update(panel=panel, band=band, period=period, metric=metric,
                                      model=model, holdout_start=boundary.isoformat(),
                                      scheduled_dates=sum(
                                          d >= boundary if period == "holdout" else d < boundary
                                          for d in schedule),
                                      candidate=units["candidate"].mean(),
                                      baseline=units["baseline"].mean(),
                                      matched_contract_cells=units["matched_contract_cells"].sum())
                        results.append(result)
    return results


def calibration_bins(path: Path) -> list[dict]:
    source = pl.scan_parquet(path)
    names = source.collect_schema().names()
    probability = "prediction" if "prediction" in names else "probability"
    observed = "itm" if "itm" in names else "observed_itm"
    if probability not in names or observed not in names:
        return []
    return source.filter(
        pl.col(probability).is_finite() & pl.col(observed).is_not_null()
    ).with_columns(
        (pl.col(probability)*10).floor().clip(0, 9).cast(pl.Int64).alias("bin")
    ).group_by("panel", "model", "side", "primary_eligible", "bin").agg(
        pl.len().alias("contract_cells"),
        pl.col(probability).mean().alias("forecast_mean"),
        pl.col(observed).cast(pl.Float64).mean().alias("observed_rate"),
    ).sort("panel", "model", "side", "primary_eligible", "bin").collect(
        engine="streaming").to_dicts()


def strategy_inference(path: Path) -> list[dict]:
    """Exploratory independent-trade excess returns against matched stock/cash."""
    source = pl.scan_parquet(path)
    names = source.collect_schema().names()
    grouping = [c for c in ("policy", "screen", "side", "haircut", "fee",
                           "stock_slippage_bps", "quality") if c in names]
    source = source.filter(pl.col("status") == "scored")
    results = []
    calendar = SessionCalendar()
    for panel, (start, end) in PANELS.items():
        boundary = holdout_boundary(start, end, calendar)
        for band, (low, high, spacing) in BANDS.items():
            schedule = inference_schedule(start, end, spacing, calendar)
            for period in ("development", "holdout"):
                rows = source.filter(
                    (pl.col("panel") == panel) & pl.col("horizon").is_between(low, high)
                    & pl.col("origin").is_in(schedule)
                ).filter(pl.col("expiry_session") < boundary if period == "development"
                         else pl.col("origin") >= boundary)
                units = rows.group_by(*grouping, "ticker", "origin").agg(
                    pl.col("excess_return").mean().alias("delta"),
                    pl.len().alias("experiments"),
                ).collect(engine="streaming")
                for key, group in units.partition_by(grouping, as_dict=True).items():
                    result = paired_interval(group)
                    result.update(dict(zip(grouping, key, strict=True)))
                    result.update(panel=panel, band=band, period=period,
                                  metric="capital_normalized_excess_return",
                                  holdout_start=boundary.isoformat())
                    results.append(result)
    return results


def forecast_descriptives(path: Path) -> list[dict]:
    """All-session metrics and support; these unequal cohorts are never a ranking."""
    source = pl.scan_parquet(path)
    names = source.collect_schema().names()
    metrics = [name for name in (*METRICS, "expected_pnl", "realized_pnl",
                                "loss_probability", "quantile05", "atom", "quantile_atom")
               if name in names]
    primary = source.filter(pl.col("primary_eligible")).with_columns(
        pl.lit("primary").alias("quality"))
    included = source.with_columns(pl.lit("reconciliation_inclusion").alias("quality"))
    source = pl.concat([primary, included]).with_columns(
        pl.when(pl.col("horizon") == 1).then(pl.lit("1"))
        .when(pl.col("horizon") <= 5).then(pl.lit("2-5"))
        .otherwise(pl.lit("6-25")).alias("band"))
    expressions = [pl.len().alias("attempted_contract_cells")]
    for metric in metrics:
        expressions.extend([pl.col(metric).is_finite().sum().alias(f"{metric}_scored"),
                            pl.col(metric).mean().alias(f"{metric}_mean")])
    if "expected_pnl" in names and "realized_pnl" in names:
        paired = pl.col("expected_pnl").is_finite() & pl.col("realized_pnl").is_finite()
        error = pl.col("expected_pnl") - pl.col("realized_pnl")
        expressions.extend([
            paired.sum().alias("pnl_paired_cells"),
            pl.col("expected_pnl").filter(paired).mean().alias("paired_expected_pnl_mean"),
            pl.col("realized_pnl").filter(paired).mean().alias("paired_realized_pnl_mean"),
            error.filter(paired).mean().alias("paired_pnl_bias"),
            error.abs().filter(paired).mean().alias("paired_pnl_absolute_error"),
        ])
    for name in ("itm", "atm", "realized_loss", "breach_strict", "breach_inclusive"):
        if name in names:
            expressions.extend([pl.col(name).is_not_null().sum().alias(f"{name}_scored"),
                                pl.col(name).cast(pl.Float64).mean().alias(f"{name}_rate")])
    return source.group_by("panel", "model", "side", "band", "quality").agg(
        *expressions).sort("panel", "model", "side", "band", "quality").collect(
        engine="streaming").to_dicts()


def loss_calibration_bins(path: Path) -> list[dict]:
    source = pl.scan_parquet(path)
    return source.filter(
        pl.col("loss_probability").is_finite() & pl.col("realized_loss").is_not_null()
    ).with_columns(
        (pl.col("loss_probability")*10).floor().clip(0, 9).cast(pl.Int64).alias("bin")
    ).group_by("panel", "model", "side", "primary_eligible", "bin").agg(
        pl.len().alias("contract_cells"),
        pl.col("loss_probability").mean().alias("forecast_mean"),
        pl.col("realized_loss").cast(pl.Float64).mean().alias("observed_rate"),
    ).sort("panel", "model", "side", "primary_eligible", "bin").collect(
        engine="streaming").to_dicts()
