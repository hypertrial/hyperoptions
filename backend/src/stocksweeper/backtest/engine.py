"""Batched VectorBT simulations. Signals fire on the next bar's open."""

from __future__ import annotations

import numpy as np
import pandas as pd

from stocksweeper.backtest.metrics import Trade
from stocksweeper.config import Settings

CHUNK = 200


def simulate(
    index: pd.DatetimeIndex,
    open_: np.ndarray,
    close: np.ndarray,
    entries: np.ndarray,
    exits: np.ndarray,
    settings: Settings,
) -> tuple[np.ndarray, list[list[Trade]]]:
    """Return a (bars, strategies) return matrix and trades per column.

    Closed trades have ``closed`` set. A trade that is still open at the last
    bar is included so its entry fill can be marked, and its exit fields are
    unused.

    ``entries`` and ``exits`` are signals known at the close. They are shifted
    one bar and filled at that bar's open, so a signal cannot trade itself.
    """
    bars, count = entries.shape
    returns = np.zeros((bars, count), dtype=float)
    trades: list[list[Trade]] = [[] for _ in range(count)]
    open_series = pd.Series(open_, index=index, name="open")
    close_series = pd.Series(close, index=index, name="close")
    for start in range(0, count, CHUNK):
        stop = min(start + CHUNK, count)
        columns = [str(offset) for offset in range(start, stop)]
        entry_frame = pd.DataFrame(entries[:, start:stop], index=index, columns=columns)
        exit_frame = pd.DataFrame(exits[:, start:stop], index=index, columns=columns)
        entry_frame = entry_frame.shift(1).fillna(False).astype(bool)
        exit_frame = exit_frame.shift(1).fillna(False).astype(bool)
        portfolio = _portfolio(close_series, open_series, entry_frame, exit_frame, settings)
        returns[:, start:stop] = _returns(portfolio, columns)
        _collect_trades(portfolio, trades, start)
    return returns, trades


def simulate_one(
    index: pd.DatetimeIndex,
    open_: np.ndarray,
    close: np.ndarray,
    entries: np.ndarray,
    exits: np.ndarray,
    settings: Settings,
) -> tuple[np.ndarray, np.ndarray, list[Trade]]:
    """Equity, drawdown, and trades for a single strategy, next-bar fills."""
    column_entries = entries.reshape(-1, 1)
    column_exits = exits.reshape(-1, 1)
    returns, trades = simulate(index, open_, close, column_entries, column_exits, settings)
    equity = settings.backtest.initial_capital * np.cumprod(1.0 + returns[:, 0])
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    return equity, drawdown, trades[0]


def _portfolio(
    close: pd.Series,
    open_: pd.Series,
    entries: pd.DataFrame,
    exits: pd.DataFrame,
    settings: Settings,
) -> object:
    import vectorbt as vbt

    return vbt.Portfolio.from_signals(
        close,
        entries,
        exits,
        price=open_,
        init_cash=settings.backtest.initial_capital,
        size=np.inf,
        fees=settings.backtest.fees,
        slippage=settings.backtest.slippage,
        direction="longonly",
        accumulate=False,
        freq="1D",
        upon_long_conflict="exit",
    )


def _returns(portfolio: object, columns: list[str]) -> np.ndarray:
    frame = portfolio.returns()  # type: ignore[attr-defined]
    array = frame.to_numpy(dtype=float)
    if array.ndim == 1:
        array = array.reshape(-1, 1)
    if array.shape[1] != len(columns):
        raise RuntimeError(f"expected {len(columns)} return columns, got {array.shape[1]}")
    return np.nan_to_num(array, nan=0.0)


def _collect_trades(portfolio: object, trades: list[list[Trade]], offset: int) -> None:
    records = portfolio.trades.records  # type: ignore[attr-defined]
    frame = records if isinstance(records, pd.DataFrame) else pd.DataFrame(records)
    for row in frame.to_dict("records"):
        status = int(row["status"])
        if status not in (0, 1):
            continue
        is_closed = status == 1
        column = offset + int(row["col"])
        trades[column].append(
            Trade(
                entry_idx=int(row["entry_idx"]),
                exit_idx=int(row["exit_idx"]) if is_closed else -1,
                ret=float(row["return"]) if is_closed else 0.0,
                pnl=float(row["pnl"]) if is_closed else 0.0,
                entry_price=float(row["entry_price"]),
                exit_price=float(row["exit_price"]) if is_closed else 0.0,
                closed=is_closed,
            )
        )
