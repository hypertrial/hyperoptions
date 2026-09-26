"""yfinance-backed market data. The only network path in the app."""

from __future__ import annotations

from datetime import date

import polars as pl

from stocksweeper.data.frames import normalize_ohlcv

EARLIEST_RESEARCH_DATE = date(1970, 1, 1)
MAX_RESEARCH_BARS = 20_000


class YFinanceProvider:
    def fetch(
        self,
        ticker: str,
        start: date | None,
        end: date | None,
        interval: str,
    ) -> pl.DataFrame:
        import yfinance as yf

        kwargs: dict[str, object] = {
            "auto_adjust": True,
            "interval": interval,
            "start": (start or EARLIEST_RESEARCH_DATE).isoformat(),
            "timeout": 20,
            "raise_errors": True,
        }
        if end is not None:
            kwargs["end"] = end.isoformat()
        history = yf.Ticker(ticker).history(**kwargs)
        if history is None or history.empty:
            return normalize_ohlcv(pl.DataFrame(), ticker)
        if len(history) > MAX_RESEARCH_BARS:
            raise ValueError(f"{ticker} research history exceeds the bounded bar limit")
        frame = pl.from_pandas(history.reset_index())
        return normalize_ohlcv(frame, ticker)
