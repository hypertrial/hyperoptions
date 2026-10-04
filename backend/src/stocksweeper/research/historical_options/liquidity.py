"""Observed archive activity, with unknown coverage kept explicit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl


def run_liquidity(
    days: pl.DataFrame,
    stocks: pl.DataFrame,
    hours: pl.DataFrame,
    output: Path,
) -> dict[str, Any]:
    """Audit every captured hour; absence is never converted into a zero bar."""
    stock_closes = stocks.select(
        "ticker", pl.col("ts").alias("session"), pl.col("close").alias("verified_stock_close")
    )
    classified = (
        hours.lazy()
        .join(stock_closes.lazy(), on=["ticker", "session"], how="left")
        .with_columns(
            (pl.col("expiry") - pl.col("session")).dt.total_days().alias("calendar_dte"),
            (pl.col("strike") / pl.col("verified_stock_close") - 1).alias("strike_distance"),
        )
        .with_columns(
            pl.when(pl.col("calendar_dte") <= 0)
            .then(pl.lit("expired_or_same_day"))
            .when(pl.col("calendar_dte") <= 7)
            .then(pl.lit("1_7_calendar_days"))
            .when(pl.col("calendar_dte") <= 35)
            .then(pl.lit("8_35_calendar_days"))
            .otherwise(pl.lit("over_35_calendar_days"))
            .alias("dte_band"),
            pl.when(pl.col("verified_stock_close").is_null())
            .then(pl.lit("unverified_stock"))
            .when(pl.col("strike_distance") < -0.10)
            .then(pl.lit("below_minus_10pct"))
            .when(pl.col("strike_distance") < -0.025)
            .then(pl.lit("minus_10_to_minus_2_5pct"))
            .when(pl.col("strike_distance") <= 0.025)
            .then(pl.lit("within_2_5pct"))
            .when(pl.col("strike_distance") <= 0.10)
            .then(pl.lit("plus_2_5_to_plus_10pct"))
            .otherwise(pl.lit("above_plus_10pct"))
            .alias("strike_distance_band"),
        )
    )
    keys = [
        "ticker",
        "side",
        "dte_band",
        "strike_distance_band",
        "hour",
        "regular_session",
        "valid",
        "reason",
    ]
    activity = (
        classified.group_by(keys)
        .agg(
            pl.len().alias("bars"),
            pl.col("contract").n_unique().alias("observed_contracts"),
            pl.col("session").n_unique().alias("observed_sessions"),
            pl.col("volume").sum().alias("volume"),
            pl.col("transactions").sum().alias("transactions"),
            (pl.col("transactions") == 1).sum().alias("single_transaction_bars"),
            (pl.col("volume") <= 5).sum().alias("at_most_five_contract_bars"),
            pl.col("volume").median().alias("median_volume"),
        )
        .sort(keys)
        .collect(engine="streaming")
    )
    activity.write_parquet(output / "liquidity.parquet", compression="zstd")
    daily = days.join(stock_closes, on=["ticker", "session"], how="left").with_columns(
        (pl.col("expiry") - pl.col("session")).dt.total_days().alias("calendar_dte"),
        (pl.col("strike") / pl.col("verified_stock_close") - 1).alias("strike_distance"),
        (pl.col("opening_open").is_not_null() & (pl.col("opening_open") > 0)).alias(
            "opening_bucket_available"
        ),
        (pl.col("regular_hours") <= 1).alias("single_hour_contract_day"),
        (pl.col("regular_hours") <= 2).alias("sparse_contract_day"),
    )
    daily.write_parquet(output / "liquidity_contract_days.parquet", compression="zstd")
    summary = (
        daily.group_by("ticker")
        .agg(
            pl.len().alias("observed_contract_days"),
            pl.col("contract").n_unique().alias("observed_contracts"),
            pl.col("session").n_unique().alias("observed_sessions"),
            pl.col("single_hour_contract_day").sum().alias("single_hour_contract_days"),
            pl.col("sparse_contract_day").sum().alias("sparse_contract_days"),
            pl.col("opening_bucket_available").sum().alias("opening_bucket_days"),
            pl.col("reconciliation_failed").sum().alias("reconciliation_failed_days"),
            pl.col("verified_stock_close").is_null().sum().alias("unverified_stock_days"),
            pl.col("volume").sum().alias("observed_regular_volume"),
            pl.col("transactions").sum().alias("observed_regular_transactions"),
        )
        .sort("ticker")
    )
    summary.write_parquet(output / "liquidity_summary.parquet", compression="zstd")
    ticker_hours = (
        hours.group_by("ticker")
        .agg(
            pl.len().alias("archive_bars"),
            pl.col("valid").sum().alias("valid_bars"),
            (~pl.col("regular_session")).sum().alias("outside_regular_session_bars"),
            (pl.col("transactions") == 1).sum().alias("single_transaction_bars"),
            (pl.col("volume") <= 5).sum().alias("at_most_five_contract_bars"),
            pl.col("volume").median().alias("median_volume"),
            pl.col("session").min().alias("first_observed_session"),
            pl.col("session").max().alias("last_observed_session"),
        )
        .sort("ticker")
    )
    ticker_hours.write_parquet(output / "liquidity_archive_summary.parquet", compression="zstd")
    if int(activity["bars"].sum() or 0) != hours.height:
        raise ValueError("archive liquidity accounting did not reconcile")
    return {
        "archive_rows": hours.height,
        "accounted_archive_rows": int(activity["bars"].sum() or 0),
        "activity_groups": activity.height,
        "contract_days": summary.to_dicts(),
        "archive": [
            {
                **row,
                "first_observed_session": str(row["first_observed_session"]),
                "last_observed_session": str(row["last_observed_session"]),
            }
            for row in ticker_hours.to_dicts()
        ],
        "coverage_interpretation": "Observed trade activity only. Missing bars and intervals "
        "before first observation have unknown trading/listing coverage. "
        "Fetch ledgers and validation gaps "
        "are reported by snapshot source audit; today's reference catalogue is not a historical "
        "listing census. NBIS unverified 2026 stock days remain visible without strike distances.",
    }
