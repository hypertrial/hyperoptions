"""Generate strategies, backtest every ticker, score robustness, and store the run."""

from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from stocksweeper.backtest.engine import simulate
from stocksweeper.backtest.metrics import Metrics, buy_and_hold_returns, segment_metrics
from stocksweeper.config import Settings
from stocksweeper.data.store import MarketStore, describe_bars, effective_ohlcv
from stocksweeper.data.synthetic import synthetic_ohlcv
from stocksweeper.indicators.engine import ensure_indicators
from stocksweeper.indicators.names import IndicatorRequest
from stocksweeper.indicators.registry import curated_requests
from stocksweeper.storage.repo import Repository, utc_now
from stocksweeper.strategy.compiler import compile_batch
from stocksweeper.strategy.generator import generate_strategies, requests_for
from stocksweeper.strategy.model import Strategy
from stocksweeper.validation.robustness import cross_ticker_score, score_strategy
from stocksweeper.validation.sensitivity import stability_scores
from stocksweeper.validation.splits import split_segments
from stocksweeper.validation.walkforward import consistency, fold_metrics, fold_windows, reoptimized

Progress = Callable[[float, str], None]


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


def run_sweep(
    settings: Settings,
    progress: Progress | None = None,
    *,
    synthetic: bool = False,
    synthetic_bars: int = 480,
) -> str:
    """Run one research sweep. Returns the new run id.

    Synthetic mode writes deterministic bars and loosens the rejection gates so
    a local demo has something to rank. Real sweeps keep the configured gates,
    and they never download data on their own.
    """
    if synthetic:
        settings = _loosen(settings)
        _write_synthetic(settings, synthetic_bars)
    store = MarketStore(settings.resolved_data_dir())
    tickers = list(settings.market.tickers)
    for ticker in tickers:
        effective_ohlcv(store, ticker, settings)
    report(progress, 0.02, "generating strategies")
    strategies = generate_strategies(settings.generator.max_strategies, settings.generator.seed)
    if not strategies:
        raise RuntimeError("the generator produced no strategies")
    indicator_requests = [*requests_for(strategies), *curated_requests()]
    run_id = uuid.uuid4().hex[:12]
    result_rows: list[dict[str, object]] = []
    fold_rows: list[dict[str, object]] = []
    reopt_rows: list[dict[str, object]] = []
    robust_rows: list[dict[str, object]] = []
    benchmark_rows: list[dict[str, object]] = []
    qualified_inputs: list[dict[str, object]] = []
    ticker_rows: list[dict[str, object]] = []

    for index, ticker in enumerate(tickers):
        report(progress, 0.05 + 0.9 * index / len(tickers), f"backtesting {ticker}")
        evaluated = _evaluate_ticker(
            settings,
            store,
            ticker,
            strategies,
            indicator_requests,
            run_id,
        )
        result_rows.extend(evaluated.results)
        fold_rows.extend(evaluated.folds)
        reopt_rows.extend(evaluated.reopt)
        robust_rows.extend(evaluated.robust)
        qualified_inputs.extend(evaluated.qualified)
        benchmark_rows.extend(evaluated.benchmarks)
        ticker_rows.append(
            {
                "run_id": run_id,
                "ticker": ticker,
                "n_bars": evaluated.n_bars,
                "first_ts": evaluated.first_ts,
                "last_ts": evaluated.last_ts,
                "bars_hash": evaluated.bars_hash,
                "limited_history": evaluated.limited,
                "survivors": evaluated.survivors,
            }
        )

    report(progress, 0.96, "scoring cross-ticker robustness")
    full_history = sum(1 for row in ticker_rows if not row["limited_history"])
    cross_rows = _cross(run_id, qualified_inputs, settings, int(full_history))
    report(progress, 0.98, "writing results")
    Repository(settings.resolved_data_dir()).save_sweep(
        {
            "runs": [
                {
                    "id": run_id,
                    "created_at": utc_now(),
                    "config_json": json.dumps(settings.model_dump(mode="json"), default=str),
                    "status": "completed",
                    "strategy_count": len(strategies),
                    "ticker_count": len(tickers),
                }
            ],
            "strategies": [_strategy_row(strategy) for strategy in strategies],
            "results": result_rows,
            "robustness": robust_rows,
            "walk_forward": fold_rows,
            "reopt": reopt_rows,
            "cross_ticker": cross_rows,
            "benchmarks": benchmark_rows,
            "run_tickers": ticker_rows,
        }
    )
    report(progress, 1.0, "completed")
    return run_id


def _evaluate_ticker(
    settings: Settings,
    store: MarketStore,
    ticker: str,
    strategies: list[Strategy],
    indicator_requests: list[IndicatorRequest],
    run_id: str,
) -> TickerRows:
    """Backtest one ticker and return the rows a sweep stores for it."""
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


def _write_synthetic(settings: Settings, bars: int) -> None:
    store = MarketStore(settings.resolved_data_dir())
    for index, ticker in enumerate(settings.market.tickers):
        bars_frame = synthetic_ohlcv(bars, seed=1000 + index, ticker=ticker)
        store.write(bars_frame, ticker, settings.market.interval)


def _loosen(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "gates": settings.gates.model_copy(
                update={
                    "min_trades": 1,
                    "min_val_trades": 1,
                    "min_degradation": 0.0,
                    "min_stability": 0.0,
                    "max_drawdown": 0.95,
                }
            )
        }
    )


def _cross(
    run_id: str,
    rows: list[dict[str, object]],
    settings: Settings,
    full_history_tickers: int,
) -> list[dict[str, object]]:
    """Qualify on full-history tickers. Limited-history names still affect the score."""
    required = min(settings.cross_ticker.min_tickers, full_history_tickers)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["strategy_id"])].append(row)
    payload: list[dict[str, object]] = []
    for strategy_id, items in grouped.items():
        good = [
            item
            for item in items
            if not item["rejected"] and _required_float(item["val_sharpe"], default=0.0) > 0
        ]
        full_passes = [item for item in good if not item.get("limited")]
        if full_history_tickers == 0:
            qualified = len(good) >= 1
        else:
            qualified = len(full_passes) >= required
        per_ticker = {
            str(item["ticker"]): {
                "score": item["score"],
                "rejected": item["rejected"],
                "sharpe": item["val_sharpe"],
                "limited": bool(item.get("limited")),
            }
            for item in items
        }
        payload.append(
            {
                "run_id": run_id,
                "strategy_id": strategy_id,
                "cross_score": cross_ticker_score(
                    [_required_float(item["score"]) for item in good],
                    settings.cross_ticker.penalty_k,
                ),
                "per_ticker_json": json.dumps(per_ticker),
                "qualified": qualified,
            }
        )
    return payload


def _strategy_row(strategy: Strategy) -> dict[str, object]:
    return {
        "id": strategy.id,
        "name": strategy.name,
        "family": strategy.family,
        "definition_json": strategy.model_dump_json(),
        "signals": strategy.signals,
    }


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


def report(progress: Progress | None, fraction: float, message: str) -> None:
    if progress is not None:
        progress(fraction, message)
