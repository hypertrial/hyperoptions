import numpy as np
import polars as pl

from stocksweeper.data.synthetic import synthetic_ohlcv
from stocksweeper.indicators.engine import compute_indicators
from stocksweeper.indicators.registry import curated_requests, output_columns


def test_every_indicator_is_look_ahead_safe():
    full = synthetic_ohlcv(280, seed=11, ticker="IREN")
    prefix = full.head(180)
    requests = curated_requests()
    full_frame = compute_indicators(full, requests).to_pandas()
    prefix_frame = compute_indicators(prefix, requests).to_pandas()
    for request in requests:
        for column in output_columns(request):
            left = prefix_frame[column].to_numpy(dtype=float)
            right = full_frame[column].to_numpy(dtype=float)[: len(left)]
            mask = np.isfinite(left) & np.isfinite(right)
            assert mask.any(), column
            np.testing.assert_allclose(
                left[mask], right[mask], rtol=1e-8, atol=1e-8, err_msg=column
            )


def test_indicator_cache_adds_missing_columns(tmp_path):
    from stocksweeper.indicators.engine import ensure_indicators
    from stocksweeper.indicators.names import IndicatorRequest

    bars = synthetic_ohlcv(80, seed=4, ticker="WULF")
    path = tmp_path / "1d.parquet"
    first = IndicatorRequest.make("rsi", {"length": 14})
    cached = ensure_indicators(bars, [first], path)
    assert "rsi_14" in cached.columns
    second = IndicatorRequest.make("roc", {"length": 10})
    updated = ensure_indicators(bars, [first, second], path)
    assert "rsi_14" in updated.columns
    assert "roc_10" in updated.columns
    assert updated.height == bars.height
    reloaded = pl.read_parquet(path)
    assert "roc_10" in reloaded.columns


def test_indicator_cache_adds_obv_average_without_duplicating_obv(tmp_path):
    from stocksweeper.indicators.engine import ensure_indicators
    from stocksweeper.indicators.names import IndicatorRequest

    bars = synthetic_ohlcv(80, seed=4, ticker="WULF")
    path = tmp_path / "1d.parquet"
    obv = IndicatorRequest.make("obv", {})
    ensure_indicators(bars, [obv], path)
    average = IndicatorRequest.make("obv_sma", {"length": 20})
    updated = ensure_indicators(bars, [obv, average], path)
    assert updated.columns.count("obv") == 1
    assert "obv_sma_20" in updated.columns


def test_indicator_cache_recomputes_when_prices_change(tmp_path):
    from stocksweeper.indicators.engine import ensure_indicators
    from stocksweeper.indicators.names import IndicatorRequest

    bars = synthetic_ohlcv(80, seed=4, ticker="WULF")
    path = tmp_path / "1d.parquet"
    request = IndicatorRequest.make("rsi", {"length": 14})
    cached = ensure_indicators(bars, [request], path)
    changed = bars.with_columns(
        pl.when(pl.col("ts") == bars["ts"][40])
        .then(pl.lit(float(bars["close"][40]) * 3))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    updated = ensure_indicators(changed, [request], path)
    fresh = compute_indicators(changed, [request])
    assert updated["rsi_14"][40] != cached["rsi_14"][40]
    assert updated.select("rsi_14").equals(fresh.select("rsi_14"))


# Columns produced for the curated set plus a 2,000-strategy sample.
# OHLCV adds seven columns, so a cache of these is 53 wide.
_CACHE_COLUMNS = {
    "adx_14",
    "aroon_14_down",
    "aroon_14_up",
    "aroon_25_down",
    "aroon_25_up",
    "atr_14",
    "atr_14_pct",
    "bb_20_2.0_lower",
    "bb_20_2.0_mid",
    "bb_20_2.0_upper",
    "cdl_doji_10_0.1",
    "cdl_inside",
    "cdl_z_30",
    "cmf_20",
    "donchian_20_lower",
    "donchian_20_mid",
    "donchian_20_upper",
    "donchian_55_lower",
    "donchian_55_mid",
    "donchian_55_upper",
    "ema_10",
    "ema_100",
    "ema_20",
    "ema_50",
    "kc_20_2.0_lower",
    "kc_20_2.0_mid",
    "kc_20_2.0_upper",
    "macd_12_9_26_hist",
    "mfi_14",
    "obv",
    "obv_sma_20",
    "psar_0.02_0.2_dir",
    "roc_10",
    "roc_20",
    "rsi_14",
    "sma_100",
    "sma_20",
    "sma_200",
    "sma_50",
    "stoch_14_3_3_k",
    "supertrend_10_2.0_dir",
    "supertrend_10_3.0_dir",
    "supertrend_14_2.0_dir",
    "supertrend_14_3.0_dir",
    "vwap_20",
    "zscore_20",
}


def test_bar_change_keeps_every_cached_column(tmp_path):
    from stocksweeper.indicators.engine import ensure_indicators
    from stocksweeper.indicators.names import IndicatorRequest
    from stocksweeper.strategy.generator import generate_strategies, requests_for

    bars = synthetic_ohlcv(90, seed=6, ticker="IREN")
    path = tmp_path / "IREN" / "1d.parquet"
    wide = [*requests_for(generate_strategies(2000, 7)), *curated_requests()]
    cached = ensure_indicators(bars, wide, path)
    grown = pl.concat(
        [bars, bars.tail(1).with_columns((pl.col("ts") + pl.duration(days=1)).alias("ts"))]
    )
    updated = ensure_indicators(grown, [IndicatorRequest.make("rsi", {"length": 14})], path)
    assert updated.width == cached.width
    assert set(updated.columns) == set(cached.columns)
    assert updated.height == cached.height + 1


def test_indicator_declaration_matches_the_compute_map_and_cache_columns():
    from stocksweeper.indicators.names import INDICATORS
    from stocksweeper.indicators.registry import SPECS
    from stocksweeper.strategy.generator import generate_strategies, requests_for

    assert set(SPECS) == set(INDICATORS)
    columns: set[str] = set()
    requests = [
        *requests_for(generate_strategies(2000, 7)),
        *curated_requests(),
    ]
    for request in requests:
        columns.update(output_columns(request))
    assert columns == _CACHE_COLUMNS
