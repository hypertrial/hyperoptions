"""Explicit market-data refresh. This is the only path that calls the provider."""

from __future__ import annotations

from collections.abc import Callable

from stocksweeper.config import Settings
from stocksweeper.data.provider import MarketDataProvider
from stocksweeper.data.store import MarketStore
from stocksweeper.data.yfinance_provider import YFinanceProvider

Progress = Callable[[float, str], None]


def update_market_data(
    settings: Settings,
    *,
    full_refresh: bool = False,
    tickers: list[str] | None = None,
    provider: MarketDataProvider | None = None,
    progress: Progress | None = None,
) -> None:
    source = provider or YFinanceProvider()
    store = MarketStore(settings.resolved_data_dir())
    chosen = tickers or list(settings.market.tickers)
    for index, ticker in enumerate(chosen):
        if progress is not None:
            progress(index / max(len(chosen), 1), f"updating {ticker}")
        store.update(
            source,
            ticker,
            interval=settings.market.interval,
            overlap_bars=settings.market.overlap_bars,
            full_refresh=full_refresh,
        )
    if progress is not None:
        progress(1, "data updated")
