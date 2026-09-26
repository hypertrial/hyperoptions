"""Deterministic daily bars for tests and the local demo seed."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from stocksweeper.data.frames import normalize_ohlcv


def synthetic_ohlcv(
    n: int,
    *,
    seed: int,
    ticker: str,
    end: date | None = None,
) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    step = np.arange(n, dtype=float)
    noise = np.cumsum(rng.normal(0.0, 0.004, n))
    close = 80.0 * np.exp(0.0007 * step + 0.07 * np.sin(2 * np.pi * step / 28.0) + noise)
    open_ = close * (1.0 + rng.normal(0.0, 0.002, n))
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.001, 0.012, n))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.001, 0.012, n))
    volume = rng.integers(1_000_000, 6_000_000, n).astype(float)
    last = end or date(2024, 12, 31)
    dates = [last - timedelta(days=n - 1 - index) for index in range(n)]
    frame = pl.DataFrame(
        {
            "ts": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "ticker": [ticker] * n,
        }
    )
    return normalize_ohlcv(frame, ticker)
