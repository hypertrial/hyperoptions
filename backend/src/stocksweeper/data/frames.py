"""Shared OHLCV frame helpers."""

from __future__ import annotations

import polars as pl

OHLCV_COLUMNS = ("ts", "open", "high", "low", "close", "volume", "ticker")


def empty_ohlcv() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "ts": pl.Date,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
            "ticker": pl.Utf8,
        }
    )


def normalize_ohlcv(frame: pl.DataFrame, ticker: str) -> pl.DataFrame:
    if frame.is_empty():
        return empty_ohlcv()
    renamed = {column: column.lower() for column in frame.columns}
    data = frame.rename(renamed)
    if "ts" not in data.columns:
        for candidate in ("date", "datetime", "index"):
            if candidate in data.columns:
                data = data.rename({candidate: "ts"})
                break
    required = ("ts", "open", "high", "low", "close", "volume")
    missing = [column for column in required if column not in data.columns]
    if missing:
        raise ValueError(f"{ticker} frame is missing columns: {', '.join(missing)}")
    out = data.select(["ts", "open", "high", "low", "close", "volume"]).with_columns(
        pl.col("ts").cast(pl.Date),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
        pl.lit(ticker).alias("ticker"),
    )
    return out.drop_nulls(subset=["ts", "close"]).unique(subset=["ts"], keep="last").sort("ts")
