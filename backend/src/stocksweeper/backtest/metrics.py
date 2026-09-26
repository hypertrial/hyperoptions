"""Performance statistics computed from returns and closed trades.

Sharpe and Sortino use the configured periods-per-year (252 daily bars) and a
zero risk-free rate. Undefined ratios stay missing instead of becoming infinity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from stocksweeper.config import BacktestSettings


@dataclass(frozen=True)
class Trade:
    entry_idx: int
    exit_idx: int
    ret: float
    pnl: float
    entry_price: float
    exit_price: float
    closed: bool = True


@dataclass(frozen=True)
class Metrics:
    cagr: float | None
    total_return: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float | None
    calmar: float | None
    win_rate: float | None
    profit_factor: float | None
    avg_trade: float | None
    median_trade: float | None
    n_trades: int
    exposure: float | None
    avg_holding_period: float | None


def metrics_from_returns(
    returns: np.ndarray,
    trades: list[Trade],
    *,
    periods_per_year: int,
    segment: tuple[int, int] | None = None,
) -> Metrics:
    start, end = (0, len(returns)) if segment is None else segment
    entries, exits, rets = _trade_columns(_closed_trades(trades))
    return _window_metrics(returns, entries, exits, rets, start, end, periods_per_year)


def segment_metrics(
    returns: np.ndarray,
    trades: list[Trade],
    bounds: dict[str, tuple[int, int]],
    periods_per_year: int,
) -> dict[str, Metrics]:
    """Metrics for every named slice, scanning the trade list once."""
    entries, exits, rets = _trade_columns(_closed_trades(trades))
    return {
        name: _window_metrics(returns, entries, exits, rets, start, end, periods_per_year)
        for name, (start, end) in bounds.items()
    }


def _closed_trades(trades: list[Trade]) -> list[Trade]:
    return [trade for trade in trades if trade.closed]


def _trade_columns(trades: list[Trade]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not trades:
        empty_idx = np.array([], dtype=int)
        return empty_idx, empty_idx.copy(), np.array([], dtype=float)
    entries = np.fromiter((trade.entry_idx for trade in trades), dtype=int, count=len(trades))
    exits = np.fromiter((trade.exit_idx for trade in trades), dtype=int, count=len(trades))
    rets = np.fromiter((trade.ret for trade in trades), dtype=float, count=len(trades))
    return entries, exits, rets


def _window_metrics(
    returns: np.ndarray,
    entries: np.ndarray,
    exits: np.ndarray,
    rets: np.ndarray,
    start: int,
    end: int,
    periods_per_year: int,
) -> Metrics:
    window = np.nan_to_num(np.asarray(returns[start:end], dtype=float), nan=0.0)
    closed = (exits >= start) & (exits < end) if len(exits) else np.array([], dtype=bool)
    selected = rets[closed]
    total = _total_return(window)
    cagr = _cagr(total, len(window), periods_per_year)
    sharpe = _sharpe(window, periods_per_year)
    sortino = _sortino(window, periods_per_year)
    drawdown = _max_drawdown(window)
    calmar = None if cagr is None or drawdown is None or drawdown == 0 else cagr / abs(drawdown)
    wins = selected[selected > 0]
    losses = selected[selected < 0]
    profit_factor = None
    if len(losses) and np.sum(np.abs(losses)) > 0:
        profit_factor = float(np.sum(wins) / np.sum(np.abs(losses))) if len(wins) else 0.0
    holding = exits[closed] - entries[closed]
    return Metrics(
        cagr=cagr,
        total_return=total,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=drawdown,
        calmar=_finite(calmar),
        win_rate=float(np.mean(selected > 0)) if len(selected) else None,
        profit_factor=_finite(profit_factor),
        avg_trade=float(np.mean(selected)) if len(selected) else None,
        median_trade=float(np.median(selected)) if len(selected) else None,
        n_trades=int(len(selected)),
        exposure=_exposure_arrays(entries, exits, start, end),
        avg_holding_period=float(np.mean(holding)) if len(holding) else None,
    )


def buy_and_hold_returns(
    open_: np.ndarray,
    close: np.ndarray,
    settings: BacktestSettings,
) -> np.ndarray:
    """Mark-to-market a single long opened at the first open, with entry costs."""
    if len(close) == 0:
        return np.array([], dtype=float)
    entry = float(open_[0]) * (1.0 + settings.slippage)
    if entry <= 0:
        return np.zeros(len(close))
    equity = (1.0 - settings.fees) * close / entry
    previous = np.empty_like(equity)
    previous[0] = 1.0
    previous[1:] = equity[:-1]
    return equity / previous - 1.0


def sharpe_matrix(returns: np.ndarray, periods_per_year: int) -> np.ndarray:
    """Column Sharpe of a (bars, strategies) return matrix."""
    if returns.size == 0:
        return np.array([])
    mean = np.nanmean(returns, axis=0)
    std = np.nanstd(returns, axis=0, ddof=1)
    out = np.full(mean.shape, np.nan)
    valid = np.isfinite(std) & (std > 0) & np.isfinite(mean)
    out[valid] = mean[valid] / std[valid] * math.sqrt(periods_per_year)
    return out


def _total_return(returns: np.ndarray) -> float | None:
    if len(returns) == 0:
        return None
    equity = float(np.prod(1.0 + returns))
    if not math.isfinite(equity) or equity < 0:
        return None
    return equity - 1.0


def _cagr(total_return: float | None, bars: int, periods_per_year: int) -> float | None:
    if total_return is None or bars <= 0 or total_return <= -1:
        return None
    years = bars / periods_per_year
    if years <= 0:
        return None
    return float((1.0 + total_return) ** (1.0 / years) - 1.0)


def _sharpe(returns: np.ndarray, periods_per_year: int) -> float | None:
    if len(returns) < 2:
        return None
    std = float(np.std(returns, ddof=1))
    if std == 0 or not math.isfinite(std):
        return None
    return float(np.mean(returns) / std * math.sqrt(periods_per_year))


def _sortino(returns: np.ndarray, periods_per_year: int) -> float | None:
    if len(returns) == 0:
        return None
    downside = float(np.sqrt(np.mean(np.minimum(returns, 0.0) ** 2)))
    if downside == 0 or not math.isfinite(downside):
        return None
    return float(np.mean(returns) / downside * math.sqrt(periods_per_year))


def _max_drawdown(returns: np.ndarray) -> float | None:
    if len(returns) == 0:
        return None
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    return float(np.min(drawdown))


def _exposure_arrays(entries: np.ndarray, exits: np.ndarray, start: int, end: int) -> float | None:
    length = end - start
    if length <= 0:
        return None
    if len(entries) == 0:
        return 0.0
    overlap = np.minimum(exits, end) - np.maximum(entries, start)
    return float(np.clip(overlap, 0, None).sum() / length)


def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(value)
