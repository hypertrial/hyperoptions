from datetime import date

import polars as pl

from stocksweeper.config import load_settings
from stocksweeper.data.store import MarketStore, effective_ohlcv
from stocksweeper.data.synthetic import synthetic_ohlcv
from stocksweeper.validation.splits import split_segments
from .conftest import FakeProvider


def test_incremental_update_refetches_only_the_tail(tmp_path):
    history = synthetic_ohlcv(40, seed=1, ticker="IREN")
    corrected = history.with_columns(
        pl.when(pl.col("ts") == history["ts"][30])
        .then(pl.lit(123.0))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    provider = FakeProvider({"IREN": corrected})
    store = MarketStore(tmp_path)
    store.write(history.head(30), "IREN")

    result = store.update(provider, "IREN", overlap_bars=5, full_refresh=False)

    assert result.requested_start == history["ts"][25]
    assert provider.calls[0][1] == history["ts"][25]
    stored = store.read("IREN")
    assert stored is not None
    assert stored.height == 40
    overlap = stored.filter(pl.col("ts") == history["ts"][30])
    assert overlap["close"][0] == 123.0


def test_full_refresh_is_explicit(tmp_path):
    history = synthetic_ohlcv(15, seed=2, ticker="CIFR")
    provider = FakeProvider({"CIFR": history})
    store = MarketStore(tmp_path)
    store.write(history.head(10), "CIFR")
    result = store.update(provider, "CIFR", full_refresh=True)
    assert result.requested_start is None
    assert provider.calls[0][1] is None
    assert store.read("CIFR").height == 15  # type: ignore[union-attr]


def test_default_config_starts_wulf_at_the_merger_close():
    assert load_settings().market.start_dates["WULF"] == date(2021, 12, 13)


def test_default_strategy_cap_matches_the_request_maximum():
    assert load_settings().generator.max_strategies == 20000


def test_effective_bars_drop_history_before_the_start_date(tmp_path):
    history = synthetic_ohlcv(40, seed=4, ticker="WULF", end=date(2022, 1, 20))
    store = MarketStore(tmp_path)
    store.write(history, "WULF")
    settings = load_settings().model_copy(
        update={
            "data_dir": tmp_path,
            "market": load_settings().market.model_copy(
                update={"tickers": ["WULF"], "start_dates": {"WULF": date(2021, 12, 13)}}
            ),
        }
    )
    trimmed = effective_ohlcv(store, "WULF", settings)
    assert trimmed.filter(pl.col("ts") < date(2021, 12, 13)).is_empty()
    assert trimmed.height < history.height
    assert store.read("WULF").height == history.height  # type: ignore[union-attr]
    _limited, bounds = split_segments(trimmed.height, settings.validation)
    assert bounds["train"][0] == 0
    assert trimmed["ts"][bounds["train"][0]] >= date(2021, 12, 13)
    status = store.status(["WULF"], start_dates=settings.market.start_dates)
    assert status[0].first == trimmed["ts"][0]
    assert status[0].bars == trimmed.height


def test_start_date_after_the_last_bar_is_an_error(tmp_path):
    history = synthetic_ohlcv(5, seed=5, ticker="WULF", end=date(2020, 1, 5))
    store = MarketStore(tmp_path)
    store.write(history, "WULF")
    settings = load_settings().model_copy(
        update={
            "market": load_settings().market.model_copy(
                update={"start_dates": {"WULF": date(2021, 12, 13)}}
            )
        }
    )
    try:
        effective_ohlcv(store, "WULF", settings)
    except FileNotFoundError as exc:
        assert "2021-12-13" in str(exc)
    else:
        raise AssertionError("expected no bars after the start date")


def test_status_reports_empty_tickers(tmp_path):
    store = MarketStore(tmp_path)
    status = store.status(["IREN"])
    assert status[0].bars == 0
    assert status[0].first is None
    assert isinstance(status[0].last, date | None.__class__) or status[0].last is None
