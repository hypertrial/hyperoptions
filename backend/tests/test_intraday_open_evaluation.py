from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from scripts.evaluate_intraday_open import evaluate
from stocksweeper.forecast.market import ForecastPriceStore
from .test_physical_contest import _bars


@pytest.mark.parametrize("close", [None, float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_invalid_maturity_close_is_never_scored(tmp_path, close):
    end = date(2026, 9, 25)
    frame = _bars(100, end).with_columns((pl.col("close") * .99).alias("open"))
    frame = frame.with_columns(
        pl.when(pl.col("ts") == end).then(pl.lit(close, dtype=pl.Float64))
        .otherwise(pl.col("close")).alias("close")
    )

    class Provider:
        def fetch(self, ticker, start, end):
            return frame

    ForecastPriceStore(tmp_path, Provider()).update("AAPL", end)
    result = evaluate("AAPL", tmp_path, 1)
    assert result["horizons"] == {}
    assert result["rejection_reasons"] == {"maturity_close_invalid": 1}


def test_valid_maturity_close_retains_paired_scoring(tmp_path):
    end = date(2026, 9, 25)
    frame = _bars(100, end).with_columns((pl.col("close") * .99).alias("open"))

    class Provider:
        def fetch(self, ticker, start, end):
            return frame

    ForecastPriceStore(tmp_path, Provider()).update("AAPL", end)
    result = evaluate("AAPL", tmp_path, 1)
    assert result["horizons"]["1"]["scored_ticker_origin_horizon_units"] == 1
    assert result["rejection_reasons"] == {}


def test_invalid_maturity_excludes_only_affected_units(tmp_path):
    end = date(2026, 9, 25)
    frame = _bars(100, end).with_columns((pl.col("close") * .99).alias("open"))
    frame = frame.with_columns(
        pl.when(pl.col("ts") == end).then(float("nan"))
        .otherwise(pl.col("close")).alias("close")
    )

    class Provider:
        def fetch(self, ticker, start, end):
            return frame

    ForecastPriceStore(tmp_path, Provider()).update("AAPL", end)
    result = evaluate("AAPL", tmp_path, 2)
    assert result["horizons"]["1"]["scored_ticker_origin_horizon_units"] == 1
    assert "2" not in result["horizons"]
    assert result["rejection_reasons"] == {"maturity_close_invalid": 2}
