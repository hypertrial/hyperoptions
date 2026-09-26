"""Evaluate the fixed strategy catalog for watchlist forecast selection."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from stocksweeper.backtest.engine import simulate
from stocksweeper.backtest.metrics import Metrics, buy_and_hold_returns, segment_metrics
from stocksweeper.config import Settings
from stocksweeper.data.store import MarketStore, describe_bars, effective_ohlcv
from stocksweeper.indicators.engine import ensure_indicators
from stocksweeper.indicators.names import IndicatorRequest
from stocksweeper.strategy.compiler import compile_batch
from stocksweeper.strategy.model import Strategy
from stocksweeper.validation.robustness import score_strategy
from stocksweeper.validation.sensitivity import stability_scores
from stocksweeper.validation.splits import split_segments
from stocksweeper.validation.walkforward import consistency, fold_metrics, fold_windows, reoptimized


@dataclass
class TickerRows:
    results: list[dict[str, object]] = field(default_factory=list)
    folds: list[dict[str, object]] = field(default_factory=list)
    reopt: list[dict[str, object]] = field(default_factory=list)
    robust: list[dict[str, object]] = field(default_factory=list)
    qualified: list[dict[str, object]] = field(default_factory=list)
    benchmarks: list[dict[str, object]] = field(default_factory=list)
    limited: bool = False
    survivors: int = 0
    n_bars: int = 0
    first_ts: object = None
    last_ts: object = None
    bars_hash: str = ""


METRIC_FIELDS = (
    "cagr", "total_return", "sharpe", "sortino", "max_drawdown", "calmar",
    "win_rate", "profit_factor", "avg_trade", "median_trade", "n_trades",
    "exposure", "avg_holding_period",
)


def _evaluate_ticker(
    settings: Settings,
    store: MarketStore,
    ticker: str,
    strategies: list[Strategy],
    indicator_requests: list[IndicatorRequest],
    run_id: str,
) -> TickerRows:
    """Score candidate rules against one ticker's selection history."""
    periods = settings.backtest.periods_per_year
    ohlcv = effective_ohlcv(store, ticker, settings)
    n_bars, first_ts, last_ts, bars_hash = describe_bars(ohlcv)
    frame = ensure_indicators(
        ohlcv, indicator_requests, store.indicator_path(ticker, settings.market.interval)
    )
    pdf = frame.to_pandas()
    entries, exits = compile_batch(pdf, strategies)
    timestamps = pd.DatetimeIndex(pd.to_datetime(pdf["ts"]))
    opened = pdf["open"].to_numpy(dtype=float)
    closed = pdf["close"].to_numpy(dtype=float)
    returns, trade_lists = simulate(timestamps, opened, closed, entries, exits, settings)
    limited, bounds = split_segments(len(pdf), settings.validation)
    segments = {**bounds, "full": (0, len(pdf))}
    windows = fold_windows(bounds["test"][0], settings.validation.wf_folds)
    is_sharpe, oos_sharpe, oos_return = fold_metrics(returns, windows, periods)
    by_strategy = [
        segment_metrics(returns[:, column], trade_lists[column], segments, periods)
        for column in range(len(strategies))
    ]
    validation_sharpe = {
        strategy.id: by_strategy[column]["validation"].sharpe
        for column, strategy in enumerate(strategies)
    }
    stability = stability_scores(strategies, validation_sharpe)
    rows = TickerRows(
        limited=limited,
        n_bars=n_bars,
        first_ts=first_ts,
        last_ts=last_ts,
        bars_hash=bars_hash,
    )
    groups: dict[str, list[int]] = defaultdict(list)
    for column, strategy in enumerate(strategies):
        groups[strategy.family].append(column)
    for family, picks in reoptimized(is_sharpe, oos_sharpe, oos_return, groups).items():
        for fold, column, fold_sharpe, fold_return in picks:
            rows.reopt.append(
                {
                    "run_id": run_id,
                    "ticker": ticker,
                    "family": family,
                    "fold": fold,
                    "strategy_id": strategies[column].id,
                    "oos_sharpe": fold_sharpe,
                    "oos_return": fold_return,
                }
            )
    for column, strategy in enumerate(strategies):
        by_segment = by_strategy[column]
        for segment_name, metrics in by_segment.items():
            rows.results.append(
                {
                    "run_id": run_id,
                    "strategy_id": strategy.id,
                    "ticker": ticker,
                    "segment": segment_name,
                    **_metrics(metrics),
                }
            )
        fold_sharpes = oos_sharpe[:, column] if len(windows) else np.array([])
        for fold in range(len(windows)):
            rows.folds.append(
                {
                    "run_id": run_id,
                    "strategy_id": strategy.id,
                    "ticker": ticker,
                    "fold": fold + 1,
                    "is_sharpe": _number(is_sharpe[fold, column]),
                    "oos_sharpe": _number(oos_sharpe[fold, column]),
                    "oos_return": _number(oos_return[fold, column]),
                }
            )
        scored = score_strategy(
            by_segment["train"],
            by_segment["validation"],
            consistency=consistency(fold_sharpes),
            stability=stability[strategy.id].score,
            neighbours=stability[strategy.id].neighbours,
            weights=settings.robustness,
            gates=settings.gates,
        )
        rows.qualified.append(
            {
                "run_id": run_id,
                "strategy_id": strategy.id,
                "ticker": ticker,
                "score": scored.score,
                "sharpe_component": scored.sharpe_component,
                "cagr_component": scored.cagr_component,
                "drawdown_component": scored.drawdown_component,
                "profit_factor_component": scored.profit_factor_component,
                "trade_component": scored.trade_component,
                "walk_forward_component": scored.walk_forward_component,
                "stability_component": scored.stability_component,
                "degradation": scored.degradation,
                "flags_json": json.dumps(scored.flags),
                "rejected": scored.rejected,
                "rank": None,
                "val_sharpe": by_segment["validation"].sharpe,
                "limited": limited,
            }
        )
    survivors = [row for row in rows.qualified if not row["rejected"]]
    survivors.sort(key=lambda row: (-_required_float(row["score"]), str(row["strategy_id"])))
    for rank, row in enumerate(survivors, start=1):
        row["rank"] = rank
    rows.survivors = len(survivors)
    for row in rows.qualified:
        stored = dict(row)
        stored.pop("val_sharpe")
        stored.pop("limited")
        rows.robust.append(stored)
    hold = buy_and_hold_returns(opened, closed, settings.backtest)
    for segment_name, segment_bounds in segments.items():
        metrics = segment_metrics(hold, [], {segment_name: segment_bounds}, periods)[segment_name]
        rows.benchmarks.append(
            {
                "run_id": run_id,
                "ticker": ticker,
                "segment": segment_name,
                "cagr": metrics.cagr,
                "total_return": metrics.total_return,
                "sharpe": metrics.sharpe,
                "sortino": metrics.sortino,
                "max_drawdown": metrics.max_drawdown,
                "calmar": metrics.calmar,
            }
        )
    return rows


def _metrics(metrics: Metrics) -> dict[str, object]:
    return {field: getattr(metrics, field) for field in METRIC_FIELDS}


def _required_float(value: object, default: float | None = None) -> float:
    if isinstance(value, int | float) and math.isfinite(value):
        return float(value)
    if default is not None:
        return default
    raise TypeError(f"expected a number, got {value!r}")


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number
