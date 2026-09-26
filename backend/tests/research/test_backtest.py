import numpy as np
import pandas as pd

from stocksweeper.backtest.engine import simulate
from stocksweeper.backtest.metrics import metrics_from_returns
from stocksweeper.config import load_settings
from stocksweeper.pipeline.detail import _fill_marks


def test_orders_fill_on_the_next_bar_open():
    index = pd.bdate_range("2020-01-01", periods=6)
    open_ = [10.0, 10.0, 20.0, 20.0, 20.0, 20.0]
    close = [10.0, 11.0, 20.0, 20.0, 20.0, 21.0]
    entries = pd.DataFrame({"signal": [False, True, False, False, False, False]}).to_numpy()
    exits = pd.DataFrame({"signal": [False, False, False, False, True, False]}).to_numpy()
    settings = load_settings()
    settings = settings.model_copy(
        update={"backtest": settings.backtest.model_copy(update={"fees": 0.0, "slippage": 0.0})}
    )
    opened = pd.Series(open_).to_numpy()
    closed = pd.Series(close).to_numpy()
    _, trades = simulate(index, opened, closed, entries, exits, settings)
    assert len(trades[0]) == 1
    trade = trades[0][0]
    assert trade.closed is True
    assert trade.entry_idx == 2
    assert trade.entry_price == 20.0
    assert index[trade.entry_idx] > index[1]


def test_open_trade_keeps_the_entry_mark_out_of_closed_stats():
    index = pd.bdate_range("2024-01-02", periods=8)
    opened = np.arange(10.0, 18.0)
    closed = opened + 1.0
    entries = np.zeros((8, 1), dtype=bool)
    exits = np.zeros((8, 1), dtype=bool)
    entries[1, 0] = True
    returns, trades = simulate(index, opened, closed, entries, exits, load_settings())
    assert int(np.count_nonzero(returns[:, 0])) > 0
    assert len(trades[0]) == 1
    trade = trades[0][0]
    assert trade.closed is False
    assert trade.entry_idx == 2
    metrics = metrics_from_returns(returns[:, 0], trades[0], periods_per_year=252)
    assert metrics.n_trades == 0
    assert metrics.win_rate is None
    assert metrics.profit_factor is None
    assert metrics.exposure == 0.0
    entry_marks = _fill_marks(8, trades[0], "entry")
    exit_marks = _fill_marks(8, trades[0], "exit")
    assert entry_marks[trade.entry_idx] == trade.entry_price
    assert all(mark is None for mark in exit_marks)
    shown = [item for item in trades[0] if item.closed and item.exit_idx < 8]
    assert shown == []
    side = "flat"
    for entry, exit_mark in zip(entry_marks, exit_marks, strict=True):
        if entry is not None:
            side = "long"
        if exit_mark is not None:
            side = "flat"
    assert side == "long"
