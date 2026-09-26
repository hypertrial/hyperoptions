"""Parquet cache for daily bars.

Historical data is never redownloaded unless ``full_refresh`` is set.
Incremental updates re-fetch a short tail so late corrections replace the overlap.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from uuid import uuid4

import numpy as np
import polars as pl
from pydantic import BaseModel

from stocksweeper.config import Settings
from stocksweeper.data.frames import normalize_ohlcv
from stocksweeper.data.provider import MarketDataProvider


class TickerStatus(BaseModel):
    ticker: str
    bars: int
    first: date | None
    last: date | None
    has_indicators: bool


class UpdateResult(BaseModel):
    ticker: str
    bars: int
    full_refresh: bool
    requested_start: date | None


class MarketStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.market_dir = data_dir / "market"
        self.indicator_dir = data_dir / "indicators"

    def path(self, ticker: str, interval: str = "1d") -> Path:
        return self.market_dir / ticker / f"{interval}.parquet"

    def indicator_path(self, ticker: str, interval: str = "1d") -> Path:
        return self.indicator_dir / ticker / f"{interval}.parquet"

    def read(self, ticker: str, interval: str = "1d") -> pl.DataFrame | None:
        path = self.path(ticker, interval)
        if not path.exists():
            return None
        return pl.read_parquet(path)

    def write(self, frame: pl.DataFrame, ticker: str, interval: str = "1d") -> None:
        path = self.path(ticker, interval)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        frame.write_parquet(temporary)
        temporary.replace(path)

    def update(
        self,
        provider: MarketDataProvider,
        ticker: str,
        *,
        interval: str = "1d",
        overlap_bars: int = 5,
        full_refresh: bool = False,
    ) -> UpdateResult:
        existing = self.read(ticker, interval)
        requested_start: date | None = None
        use_existing = existing is not None and existing.height > 0 and not full_refresh
        if use_existing and existing is not None:
            offset = min(overlap_bars, existing.height)
            requested_start = existing["ts"][existing.height - offset]
        fetched = normalize_ohlcv(
            provider.fetch(ticker, requested_start, None, interval),
            ticker,
        )
        if fetched.is_empty() and use_existing and existing is not None:
            merged = existing
        elif use_existing and existing is not None:
            merged = normalize_ohlcv(pl.concat([existing, fetched], how="vertical"), ticker)
        else:
            if fetched.is_empty():
                raise ValueError(f"no market data returned for {ticker}")
            merged = fetched
        self.write(merged, ticker, interval)
        return UpdateResult(
            ticker=ticker,
            bars=merged.height,
            full_refresh=full_refresh or not use_existing,
            requested_start=requested_start,
        )

    def status(
        self,
        tickers: list[str],
        interval: str = "1d",
        start_dates: Mapping[str, date] | None = None,
    ) -> list[TickerStatus]:
        rows: list[TickerStatus] = []
        for ticker in tickers:
            frame = self.read(ticker, interval)
            start = None if start_dates is None else start_dates.get(ticker)
            if frame is not None and start is not None:
                frame = frame.filter(pl.col("ts") >= start)
            if frame is None or frame.is_empty():
                rows.append(
                    TickerStatus(ticker=ticker, bars=0, first=None, last=None, has_indicators=False)
                )
                continue
            rows.append(
                TickerStatus(
                    ticker=ticker,
                    bars=frame.height,
                    first=frame["ts"][0],
                    last=frame["ts"][-1],
                    has_indicators=self.indicator_path(ticker, interval).exists(),
                )
            )
        return rows


def describe_bars(frame: pl.DataFrame) -> tuple[int, date, date, str]:
    """Bar count, first date, last date, and a hash of the OHLCV values."""
    ordered = frame.select(["ts", "open", "high", "low", "close", "volume"])
    days = np.ascontiguousarray(ordered["ts"].cast(pl.Int32).to_numpy())
    prices = ordered.select(["open", "high", "low", "close", "volume"]).to_numpy()
    prices = np.ascontiguousarray(prices, dtype=np.float64)
    digest = hashlib.sha256()
    digest.update(days.tobytes())
    digest.update(prices.tobytes())
    return ordered.height, ordered["ts"][0], ordered["ts"][-1], digest.hexdigest()


def require_ohlcv(store: MarketStore, ticker: str, interval: str = "1d") -> pl.DataFrame:
    frame = store.read(ticker, interval)
    if frame is None or frame.is_empty():
        raise FileNotFoundError(
            f"no local bars for {ticker}. Watchlist forecasts require prepared price history."
        )
    return frame


def effective_ohlcv(store: MarketStore, ticker: str, settings: Settings) -> pl.DataFrame:
    """Bars on or after the ticker start date. The parquet file is left as stored."""
    frame = require_ohlcv(store, ticker, settings.market.interval)
    start = settings.market.start_dates.get(ticker)
    if start is None:
        return frame
    trimmed = frame.filter(pl.col("ts") >= start)
    if trimmed.is_empty():
        raise FileNotFoundError(f"no bars for {ticker} on or after {start.isoformat()}")
    return trimmed
