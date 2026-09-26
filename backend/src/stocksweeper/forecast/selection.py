"""Freeze rule selection before any return used to calibrate the forecast."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from stocksweeper.config import Settings, load_settings
from stocksweeper.data.store import MarketStore
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import clean_completed, price_hash
from stocksweeper.forecast.models import State
from stocksweeper.strategy.model import Strategy

PREFIX_BARS = 600
EVIDENCE_BARS = 100


@lru_cache(maxsize=1)
def catalog() -> tuple[tuple[Strategy, ...], str]:
    from stocksweeper.strategy.generator import generate_strategies

    rules = tuple(generate_strategies(512, seed=7))
    if len(rules) != 512:
        raise RuntimeError("forecast catalog must have 512 rules")
    encoded = json.dumps(
        [rule.model_dump(mode="json") for rule in rules], sort_keys=True, separators=(",", ":")
    )
    # Selection results depend on indicator, compiler, validation and scoring
    # code as well as rule definitions. Invalidate cached selections on a code
    # change so a previous rule is not silently reused under a new engine.
    engine_root = Path(__file__).resolve().parents[1]
    engine_dirs = ("strategy", "indicators", "validation", "backtest")
    engine_files = (
        "config.py",
        "pipeline/sweep.py",
        "data/store.py",
        "data/frames.py",
        "forecast/market.py",
        "forecast/calendar.py",
        "forecast/selection.py",
        "forecast/samples.py",
        "forecast/calibration.py",
        "forecast/service.py",
    )
    source_hash = hashlib.sha256()
    sources = [engine_root / name for name in engine_files]
    for directory in engine_dirs:
        sources.extend((engine_root / directory).glob("*.py"))
    for path in sorted(sources):
        source_hash.update(str(path.relative_to(engine_root)).encode())
        source_hash.update(path.read_bytes())
    from options_api import market_calendar

    source_hash.update(Path(market_calendar.__file__).read_bytes())
    return rules, hashlib.sha256(encoded.encode() + source_hash.digest()).hexdigest()


@dataclass(frozen=True)
class Selection:
    strategy: Strategy
    cutoff: date
    prefix_hash: str
    catalog_hash: str
    validation_sharpe: float
    robustness: float


def select_strategy(
    ticker: str,
    bars: pl.DataFrame,
    completed: date,
    data_dir: Path,
    *,
    peer: bool = False,
    calendar: SessionCalendar | None = None,
) -> tuple[Selection | None, str | None]:
    clean = clean_completed(bars, completed, calendar or SessionCalendar())
    if clean.height < PREFIX_BARS + (0 if peer else EVIDENCE_BARS):
        return None, "ticker_history_short"
    prefix = clean.head(PREFIX_BARS)
    cutoff = prefix["ts"][-1]
    if peer and cutoff > date(2020, 12, 31):
        return None, "peer_history_short"
    prefix_hash = price_hash(prefix)
    rules, catalog_hash = catalog()
    from stocksweeper.pipeline.sweep import _evaluate_ticker
    from stocksweeper.strategy.generator import requests_for

    selection_dir = data_dir / "forecast" / "selection"
    store = MarketStore(selection_dir)
    store.write(
        prefix.select(["ts", "open", "high", "low", "close", "volume"]).with_columns(
            pl.lit(ticker).alias("ticker")
        ),
        ticker,
    )
    base = load_settings()
    settings: Settings = base.model_copy(
        update={
            "data_dir": selection_dir,
            "market": base.market.model_copy(update={"tickers": [ticker], "start_dates": {}}),
        }
    )
    evaluated = _evaluate_ticker(
        settings, store, ticker, list(rules), requests_for(list(rules)), "forecast-selection"
    )
    qualified = [
        row
        for row in evaluated.qualified
        if not row["rejected"]
        and isinstance(row["val_sharpe"], (int, float))
        and np.isfinite(row["val_sharpe"])
        and row["val_sharpe"] > 0
    ]
    if not qualified:
        return None, "ticker_strategy_unqualified"
    qualified.sort(key=lambda row: (-float(row["score"]), str(row["strategy_id"])))
    best = qualified[0]
    strategy = next(rule for rule in rules if rule.id == best["strategy_id"])
    return Selection(
        strategy=strategy,
        cutoff=cutoff,
        prefix_hash=prefix_hash,
        catalog_hash=catalog_hash,
        validation_sharpe=float(best["val_sharpe"]),
        robustness=float(best["score"]),
    ), None


def strategy_states(bars: pl.DataFrame, strategy: Strategy) -> tuple[State, ...]:
    """State intended for the next open, including today's prospective entry."""
    from stocksweeper.indicators.engine import compute_indicators
    from stocksweeper.strategy.compiler import compile_strategy
    from stocksweeper.strategy.generator import requests_for

    if bars.is_empty():
        return ()
    frame = compute_indicators(
        bars.select(["ts", "open", "high", "low", "close", "volume"]).with_columns(
            pl.lit("FORECAST").alias("ticker")
        ),
        requests_for([strategy]),
    ).to_pandas()
    # Stop-rule compilation suppresses a terminal-bar entry because backtests
    # cannot fill it. A NaN placeholder exposes the signal for the next open.
    placeholder = pd.DataFrame({name: [np.nan] for name in frame.columns})
    frame = pd.concat([frame, placeholder], ignore_index=True)
    entries, exits = compile_strategy(frame, strategy)
    held = False
    states: list[State] = []
    for entry, exit_ in zip(entries[:-1], exits[:-1], strict=True):
        if exit_:
            held = False
        elif entry:
            held = True
        states.append("long" if held else "flat")
    return tuple(states)
