"""Replaceable market-data source."""

from __future__ import annotations

from datetime import date
from typing import Protocol

import polars as pl


class MarketDataProvider(Protocol):
    def fetch(
        self,
        ticker: str,
        start: date | None,
        end: date | None,
        interval: str,
    ) -> pl.DataFrame:
        """Return normalized OHLCV columns ts, open, high, low, close, volume, ticker."""
        raise NotImplementedError
