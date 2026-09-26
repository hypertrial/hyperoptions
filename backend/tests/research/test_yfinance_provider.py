"""Network-free checks for bounded research market-data requests."""

from datetime import date

import pandas as pd
import pytest
import yfinance as yf

from stocksweeper.data.yfinance_provider import MAX_RESEARCH_BARS, YFinanceProvider


def test_full_fetch_bounds_history_and_normalizes_adjusted_bars(monkeypatch):
    calls = []
    history = pd.DataFrame(
        {
            "Open": [20.0, 10.0],
            "High": [21.0, 11.0],
            "Low": [19.0, 9.0],
            "Close": [20.5, 10.5],
            "Volume": [200, 100],
            "Dividends": [0.0, 0.0],
        },
        index=pd.DatetimeIndex(["2024-01-03", "2024-01-02"], name="Date"),
    )

    class Ticker:
        def __init__(self, symbol):
            assert symbol == "AAPL"

        def history(self, **kwargs):
            calls.append(kwargs)
            return history

    monkeypatch.setattr(yf, "Ticker", Ticker)

    result = YFinanceProvider().fetch("AAPL", None, None, "1d")

    assert calls == [
        {
            "auto_adjust": True,
            "interval": "1d",
            "start": "1970-01-01",
            "timeout": 20,
            "raise_errors": True,
        }
    ]
    assert result.columns == ["ts", "open", "high", "low", "close", "volume", "ticker"]
    assert result["ts"].to_list() == [date(2024, 1, 2), date(2024, 1, 3)]
    assert result["close"].to_list() == [10.5, 20.5]
    assert result["ticker"].to_list() == ["AAPL", "AAPL"]


def test_incremental_fetch_preserves_requested_dates(monkeypatch):
    calls = []

    class Ticker:
        def __init__(self, symbol):
            assert symbol == "MSFT"

        def history(self, **kwargs):
            calls.append(kwargs)
            return pd.DataFrame()

    monkeypatch.setattr(yf, "Ticker", Ticker)

    result = YFinanceProvider().fetch("MSFT", date(2024, 1, 4), date(2024, 2, 1), "1wk")

    assert calls == [
        {
            "auto_adjust": True,
            "interval": "1wk",
            "start": "2024-01-04",
            "end": "2024-02-01",
            "timeout": 20,
            "raise_errors": True,
        }
    ]
    assert result.is_empty()
    assert result.columns == ["ts", "open", "high", "low", "close", "volume", "ticker"]


def test_oversized_history_is_rejected_before_pandas_conversion(monkeypatch):
    class OversizedHistory:
        empty = False

        def __len__(self):
            return MAX_RESEARCH_BARS + 1

        def reset_index(self):
            raise AssertionError("oversized history must not be converted")

    class Ticker:
        def __init__(self, symbol):
            assert symbol == "AAPL"

        def history(self, **kwargs):
            return OversizedHistory()

    monkeypatch.setattr(yf, "Ticker", Ticker)

    with pytest.raises(ValueError, match="bounded bar limit"):
        YFinanceProvider().fetch("AAPL", None, None, "1d")


def test_history_at_bar_limit_is_accepted(monkeypatch):
    history = pd.DataFrame(
        {
            "Open": 1.0,
            "High": 1.0,
            "Low": 1.0,
            "Close": 1.0,
            "Volume": 100,
        },
        index=pd.date_range("1970-01-01", periods=MAX_RESEARCH_BARS, name="Date"),
    )

    class Ticker:
        def __init__(self, symbol):
            assert symbol == "AAPL"

        def history(self, **kwargs):
            return history

    monkeypatch.setattr(yf, "Ticker", Ticker)

    assert YFinanceProvider().fetch("AAPL", None, None, "1d").height == MAX_RESEARCH_BARS


def test_provider_error_is_not_silenced(monkeypatch):
    class Ticker:
        def __init__(self, symbol):
            assert symbol == "AAPL"

        def history(self, **kwargs):
            assert kwargs["raise_errors"] is True
            raise TimeoutError("provider timed out")

    monkeypatch.setattr(yf, "Ticker", Ticker)

    with pytest.raises(TimeoutError, match="provider timed out"):
        YFinanceProvider().fetch("AAPL", None, None, "1d")
