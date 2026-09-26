import numpy as np
import pandas as pd

from stocksweeper.backtest.engine import simulate, simulate_one
from stocksweeper.backtest.metrics import metrics_from_returns
from stocksweeper.config import load_settings


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


def test_single_strategy_drawdown_includes_initial_capital(monkeypatch):
    import stocksweeper.backtest.engine as engine

    monkeypatch.setattr(engine, "simulate", lambda *args: (np.array([[-0.5], [0.0]]), [[]]))
    index = pd.bdate_range("2024-01-02", periods=2)
    zeros = np.zeros(2)
    signals = np.zeros(2, dtype=bool)
    settings = load_settings()
    equity, drawdown, trades = simulate_one(index, zeros, zeros, signals, signals, settings)
    np.testing.assert_allclose(equity, [settings.backtest.initial_capital * 0.5] * 2)
    np.testing.assert_allclose(drawdown, [-0.5, -0.5])
    assert trades == []
