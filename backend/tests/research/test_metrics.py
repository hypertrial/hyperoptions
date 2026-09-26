import math

import numpy as np

from stocksweeper.backtest.metrics import (
    Trade,
    buy_and_hold_returns,
    metrics_from_returns,
    segment_metrics,
)
from stocksweeper.config import BacktestSettings


def test_metrics_match_hand_calculation():
    returns = np.array([0.10, -0.05, 0.02])
    trades = [
        Trade(0, 0, 0.20, 10, 10, 12),
        Trade(0, 1, -0.10, -5, 12, 10.8),
        Trade(1, 2, 0.10, 4, 10, 11),
    ]
    metrics = metrics_from_returns(returns, trades, periods_per_year=252)
    expected_total = 1.10 * 0.95 * 1.02 - 1
    assert metrics.total_return == pytest_approx(expected_total)
    assert metrics.cagr == pytest_approx((1 + expected_total) ** (252 / 3) - 1)
    mean = float(np.mean(returns))
    std = float(np.std(returns, ddof=1))
    assert metrics.sharpe == pytest_approx(mean / std * math.sqrt(252))
    equity = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(np.maximum(equity, 1.0))
    assert metrics.max_drawdown == pytest_approx(float(np.min(equity / peak - 1)))
    assert metrics.win_rate == pytest_approx(2 / 3)
    assert metrics.profit_factor == pytest_approx(0.30 / 0.10)
    assert metrics.n_trades == 3
    assert metrics.median_trade == pytest_approx(0.10)


def test_flat_series_has_no_sharpe_and_zero_drawdown():
    returns = np.full(252, 0.01)
    metrics = metrics_from_returns(returns, [], periods_per_year=252)
    assert metrics.sharpe is None
    assert metrics.cagr == pytest_approx(1.01**252 - 1)
    assert metrics.max_drawdown == pytest_approx(0.0)


def test_first_bar_loss_counts_toward_max_drawdown_for_full_and_segment_metrics():
    returns = np.array([-0.50, 0.0, 0.50])
    full = metrics_from_returns(returns, [], periods_per_year=252)
    first_segment = metrics_from_returns(returns, [], periods_per_year=252, segment=(0, 2))
    later_segment = metrics_from_returns(returns, [], periods_per_year=252, segment=(1, 3))
    assert full.max_drawdown == pytest_approx(-0.50)
    assert first_segment.max_drawdown == pytest_approx(-0.50)
    assert later_segment.max_drawdown == pytest_approx(0.0)


def test_segment_metrics_match_a_trade_scan_per_slice():
    rng = np.random.default_rng(4)
    returns = rng.normal(0.001, 0.02, 80)
    trades = [
        Trade(int(entry), int(exit_), float(ret), 1.0, 10.0, 11.0)
        for entry, exit_, ret in zip(
            rng.integers(0, 70, 30),
            rng.integers(5, 80, 30),
            rng.normal(0.01, 0.05, 30),
            strict=True,
        )
        if exit_ > entry
    ]
    bounds = {"train": (0, 40), "validation": (40, 60), "test": (60, 80), "full": (0, 80)}
    scored = segment_metrics(returns, trades, bounds, 252)
    for name, segment in bounds.items():
        direct = metrics_from_returns(returns, trades, periods_per_year=252, segment=segment)
        assert scored[name] == direct
        assert scored[name] == _reference_metrics(returns, trades, segment)


def _reference_metrics(returns: np.ndarray, trades: list[Trade], segment: tuple[int, int]):
    """Independent copy of the pre-refactor per-segment scan."""
    start, end = segment
    window = np.nan_to_num(np.asarray(returns[start:end], dtype=float), nan=0.0)
    selected = [trade for trade in trades if start <= trade.exit_idx < end]
    total = None if len(window) == 0 else float(np.prod(1.0 + window) - 1.0)
    rets = np.array([trade.ret for trade in selected], dtype=float)
    held = 0
    for trade in trades:
        overlap = min(trade.exit_idx, end) - max(trade.entry_idx, start)
        if overlap > 0:
            held += overlap
    exposure = None if end <= start else held / (end - start)
    direct = metrics_from_returns(returns, trades, periods_per_year=252, segment=segment)
    if total is None:
        assert direct.total_return is None
    else:
        assert direct.total_return == pytest_approx(total)
    assert direct.n_trades == len(selected)
    if exposure is None:
        assert direct.exposure is None
    else:
        assert direct.exposure == pytest_approx(exposure)
    if len(rets):
        assert direct.avg_trade == pytest_approx(float(np.mean(rets)))
    return direct


def test_buy_and_hold_charges_entry_costs():
    settings = BacktestSettings(
        initial_capital=100_000, fees=0.0, slippage=0.0, periods_per_year=252
    )
    prices = np.array([10.0, 10.0, 20.0])
    returns = buy_and_hold_returns(prices, prices, settings)
    assert returns[0] == pytest_approx(0.0)
    assert returns[-1] == pytest_approx(1.0)


def pytest_approx(value: float):
    import pytest

    return pytest.approx(value)
