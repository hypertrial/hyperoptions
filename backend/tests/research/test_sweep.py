import numpy as np
import pandas as pd

from stocksweeper.backtest.engine import simulate, simulate_one
from stocksweeper.config import load_settings
from stocksweeper.validation.walkforward import fold_metrics, fold_windows, reoptimized


def test_simulation_past_the_chunk_matches_one_column():
    bars = 36
    count = 250
    index = pd.bdate_range("2020-01-01", periods=bars)
    rng = np.random.default_rng(5)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, bars))
    opened = close * (1.0 + rng.normal(0.0, 0.001, bars))
    entries = rng.random((bars, count)) > 0.85
    exits = rng.random((bars, count)) > 0.85
    settings = load_settings().model_copy(
        update={
            "backtest": load_settings().backtest.model_copy(update={"fees": 0.0, "slippage": 0.0})
        }
    )
    returns, trades = simulate(index, opened, close, entries, exits, settings)
    for column in (0, 199, 200, 249):
        equity, _, solo = simulate_one(
            index, opened, close, entries[:, column], exits[:, column], settings
        )
        capital = settings.backtest.initial_capital
        solo_returns = np.empty(bars)
        solo_returns[0] = equity[0] / capital - 1.0
        solo_returns[1:] = equity[1:] / equity[:-1] - 1.0
        np.testing.assert_allclose(returns[:, column], solo_returns, atol=1e-12)
        assert [(trade.entry_idx, trade.exit_idx) for trade in trades[column]] == [
            (trade.entry_idx, trade.exit_idx) for trade in solo
        ]


def test_fold_metrics_follow_the_window_cuts():
    returns = np.array(
        [
            [0.01, -0.02],
            [0.02, 0.01],
            [-0.01, 0.02],
            [0.03, -0.01],
            [0.00, 0.02],
            [-0.02, 0.01],
        ]
    )
    windows = fold_windows(6, 1)
    assert windows == [(0, 3, 3, 6)]
    in_sample, _, out_return = fold_metrics(returns, windows, 252)
    assert in_sample.shape == (1, 2)
    assert np.isfinite(in_sample[0, 0])
    np.testing.assert_allclose(out_return[0], np.prod(1.0 + returns[3:6], axis=0) - 1.0)


def test_reoptimized_picks_the_best_in_sample_column():
    in_sample = np.array([[0.2, 1.5, np.nan], [np.nan, np.nan, np.nan]])
    out_sharpe = np.array([[0.3, 0.4, 0.5], [1.0, 1.0, 1.0]])
    out_return = np.array([[0.01, 0.02, 0.03], [0.1, 0.1, 0.1]])
    picked = reoptimized(in_sample, out_sharpe, out_return, {"trend": [0, 1], "other": []})
    assert picked["trend"] == [(1, 1, 0.4, 0.02)]
    assert picked["other"] == []
