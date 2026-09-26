"""Shared synthetic bars and an isolated data directory."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

import stocksweeper  # noqa: F401  configures the numba cache before indicators import
from stocksweeper.config import load_settings
from stocksweeper.data.synthetic import synthetic_ohlcv


class FakeProvider:
    def __init__(self, frames: dict[str, pl.DataFrame]) -> None:
        self.frames = frames
        self.calls: list[tuple[str, date | None, date | None, str]] = []

    def fetch(
        self, ticker: str, start: date | None, end: date | None, interval: str
    ) -> pl.DataFrame:
        self.calls.append((ticker, start, end, interval))
        frame = self.frames[ticker]
        if start is not None:
            frame = frame.filter(pl.col("ts") >= start)
        if end is not None:
            frame = frame.filter(pl.col("ts") <= end)
        return frame


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOCKSWEEPER_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def settings(data_dir: Path):
    loaded = load_settings()
    return loaded.model_copy(
        update={
            "data_dir": data_dir,
            "market": loaded.market.model_copy(update={"tickers": ["IREN", "CIFR"]}),
            "generator": loaded.generator.model_copy(update={"max_strategies": 12}),
        }
    )


@pytest.fixture
def bars() -> pl.DataFrame:
    return synthetic_ohlcv(120, seed=3, ticker="IREN")
