"""Re-simulate one stored strategy for the detail page."""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import polars as pl

from stocksweeper.backtest.engine import simulate_one
from stocksweeper.backtest.metrics import Trade, buy_and_hold_returns
from stocksweeper.config import Settings
from stocksweeper.data.store import MarketStore, describe_bars, effective_ohlcv
from stocksweeper.indicators.engine import ensure_indicators
from stocksweeper.storage.repo import Repository
from stocksweeper.strategy.compiler import compile_strategy
from stocksweeper.strategy.generator import present_signals, requests_for
from stocksweeper.strategy.model import Strategy
from stocksweeper.validation.splits import split_segments


class RunSnapshotMissing(Exception):
    """A run was stored before per-ticker bar snapshots existed."""


def load_detail(
    settings: Settings, run_id: str, strategy_id: str, ticker: str
) -> dict[str, object] | None:
    repo = Repository(settings.resolved_data_dir())
    record = repo.strategy_record(run_id, strategy_id, ticker)
    if record is None:
        return None
    strategy = Strategy.model_validate_json(str(record["definition_json"]))
    snapshot = repo.ticker_snapshot(run_id, ticker)
    if snapshot is None:
        raise RunSnapshotMissing("run predates data snapshots; run a new sweep")
    store = MarketStore(settings.resolved_data_dir())
    ohlcv = effective_ohlcv(store, ticker, settings)
    frame = ensure_indicators(
        ohlcv,
        requests_for([strategy]),
        store.indicator_path(ticker, settings.market.interval),
    )
    last_ts = _as_date(snapshot["last_ts"])
    sliced = frame.filter(pl.col("ts") <= last_ts)
    if sliced.is_empty():
        data_snapshot = "changed"
        dates: list[str] = []
        closed = np.array([], dtype=float)
        equity = np.array([], dtype=float)
        drawdown = np.array([], dtype=float)
        buy_hold = np.array([], dtype=float)
        trades: list[Trade] = []
    else:
        window = sliced.select(["ts", "open", "high", "low", "close", "volume", "ticker"])
        count, _first, _last, digest = describe_bars(window)
        matches = digest == snapshot["bars_hash"] and count == snapshot["n_bars"]
        data_snapshot = "exact" if matches else "changed"
        pdf = sliced.to_pandas()
        entries, exits = compile_strategy(pdf, strategy)
        index = pd.DatetimeIndex(pd.to_datetime(pdf["ts"]))
        opened = pdf["open"].to_numpy(dtype=float)
        closed = pdf["close"].to_numpy(dtype=float)
        equity, drawdown, trades = simulate_one(index, opened, closed, entries, exits, settings)
        hold = buy_and_hold_returns(opened, closed, settings.backtest)
        buy_hold = settings.backtest.initial_capital * np.cumprod(1.0 + hold)
        dates = [pd.Timestamp(value).date().isoformat() for value in index]
    entry_signals, filter_signals, exit_signals, exit_kind = present_signals(
        strategy.signals, strategy.filters, strategy.exit_rule.kind
    )
    benches = repo.benchmarks(run_id, ticker)
    segments = [
        _segment(row, benches.get(str(row.get("segment")), {}))
        for row in record["segments"]  # type: ignore[union-attr]
    ]
    return {
        "run_id": run_id,
        "ticker": ticker,
        "strategy_id": strategy.id,
        "name": strategy.name,
        "family": strategy.family,
        "signals": strategy.signals,
        "entry_signals": entry_signals,
        "filter_signals": filter_signals,
        "exit_signals": exit_signals,
        "exit_kind": exit_kind,
        "parameters": strategy.parameters,
        "entry": strategy.entry.model_dump(mode="json"),
        "exit": strategy.exit.model_dump(mode="json"),
        "robustness": record["score"],
        "degradation": record["degradation"],
        "stability": record["stability_component"],
        "walk_forward_consistency": record["walk_forward_component"],
        "rejected": bool(record["rejected"]),
        "flags": json.loads(str(record["flags_json"] or "[]")),
        "rank": record["rank"],
        "data_snapshot": data_snapshot,
        "segment_bounds": segment_bound_dates(dates, settings),
        "segments": segments,
        "folds": record["folds"],
        "reopt": record["reopt"],
        "dates": dates,
        "close": closed.tolist(),
        "entry_marks": _fill_marks(len(closed), trades, "entry"),
        "exit_marks": _fill_marks(len(closed), trades, "exit"),
        "equity": equity.tolist(),
        "buy_hold": buy_hold.tolist(),
        "drawdown": drawdown.tolist(),
        "trades": [
            {
                "entry_date": dates[trade.entry_idx],
                "exit_date": dates[trade.exit_idx],
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "return": trade.ret,
                "pnl": trade.pnl,
                "holding_bars": trade.exit_idx - trade.entry_idx,
            }
            for trade in trades
            if trade.closed and trade.exit_idx < len(dates)
        ],
    }


def _fill_marks(length: int, trades: list[Trade], side: str) -> list[float | None]:
    marks: list[float | None] = [None] * length
    for trade in trades:
        if side == "exit" and not trade.closed:
            continue
        index = trade.entry_idx if side == "entry" else trade.exit_idx
        price = trade.entry_price if side == "entry" else trade.exit_price
        if 0 <= index < length:
            marks[index] = float(price)
    return marks


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"expected a date, got {value!r}")


def segment_bound_dates(dates: list[str], settings: Settings) -> dict[str, dict[str, str | None]]:
    if not dates:
        return {}
    _limited, bounds = split_segments(len(dates), settings.validation)
    named: dict[str, dict[str, str | None]] = {}
    for name, (start, end) in bounds.items():
        if start >= end or start >= len(dates):
            named[name] = {"start": None, "end": None}
            continue
        last = min(end, len(dates)) - 1
        named[name] = {"start": dates[start], "end": dates[last]}
    return named


def _segment(row: dict[str, object], bench: dict[str, object] | None = None) -> dict[str, object]:
    keep = (
        "segment",
        "cagr",
        "total_return",
        "sharpe",
        "sortino",
        "max_drawdown",
        "calmar",
        "win_rate",
        "profit_factor",
        "avg_trade",
        "median_trade",
        "n_trades",
        "exposure",
        "avg_holding_period",
    )
    payload = {key: row.get(key) for key in keep}
    payload["buy_hold_cagr"] = None if bench is None else bench.get("cagr")
    payload["buy_hold_max_drawdown"] = None if bench is None else bench.get("max_drawdown")
    return payload
