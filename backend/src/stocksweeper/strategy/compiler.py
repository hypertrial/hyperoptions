"""Compile strategy conditions into boolean entry and exit arrays."""

from __future__ import annotations

import numpy as np
import pandas as pd

from stocksweeper.indicators.names import column_for
from stocksweeper.strategy.exits import apply_exit
from stocksweeper.strategy.model import Condition, ConditionGroup, Strategy


def compile_strategy(frame: pd.DataFrame, strategy: Strategy) -> tuple[np.ndarray, np.ndarray]:
    entries = _group(frame, strategy.entry)
    exits = _group(frame, strategy.exit)
    if strategy.exit_rule.kind == "mirror":
        return entries, exits
    close = frame["close"].to_numpy(dtype=float)
    return apply_exit(entries, exits, close, _atr(frame, strategy), strategy.exit_rule)


def _atr(frame: pd.DataFrame, strategy: Strategy) -> np.ndarray:
    if strategy.exit_rule.kind != "atr_trail":
        return np.zeros(len(frame), dtype=float)
    column = column_for("atr", {"length": strategy.exit_rule.atr_length})
    if column not in frame.columns:
        raise KeyError(f"indicator column {column} is not in the frame")
    return frame[column].to_numpy(dtype=float)


def compile_batch(
    frame: pd.DataFrame,
    strategies: list[Strategy],
) -> tuple[np.ndarray, np.ndarray]:
    count = len(strategies)
    entries = np.zeros((len(frame), count), dtype=bool)
    exits = np.zeros((len(frame), count), dtype=bool)
    for index, strategy in enumerate(strategies):
        entries[:, index], exits[:, index] = compile_strategy(frame, strategy)
    return entries, exits


def _group(frame: pd.DataFrame, group: ConditionGroup) -> np.ndarray:
    if not group.conditions:
        fill = group.logic == "AND"
        return np.full(len(frame), fill, dtype=bool)
    parts = np.vstack([_condition(frame, condition) for condition in group.conditions])
    if group.logic == "AND":
        return parts.all(axis=0)
    return parts.any(axis=0)


def _condition(frame: pd.DataFrame, condition: Condition) -> np.ndarray:
    left = _series(frame, condition.indicator, condition.params)
    right = _rhs(frame, condition)
    if condition.op == "gt":
        mask = left > right
    elif condition.op == "lt":
        mask = left < right
    elif condition.op == "gte":
        mask = left >= right
    elif condition.op == "lte":
        mask = left <= right
    elif condition.op == "crosses_above":
        mask = (left > right) & (np.roll(left, 1) <= np.roll(right, 1))
        mask[0] = False
    elif condition.op == "crosses_below":
        mask = (left < right) & (np.roll(left, 1) >= np.roll(right, 1))
        mask[0] = False
    else:
        raise ValueError(f"unsupported operator {condition.op}")
    valid = np.isfinite(left) & np.isfinite(right)
    return np.asarray(mask & valid, dtype=bool)


def _rhs(frame: pd.DataFrame, condition: Condition) -> np.ndarray:
    if condition.rhs.kind == "const":
        if condition.rhs.value is None:
            raise ValueError("constant comparison is missing a value")
        return np.full(len(frame), float(condition.rhs.value))
    if not condition.rhs.indicator:
        raise ValueError("indicator comparison is missing a name")
    return _series(frame, condition.rhs.indicator, condition.rhs.params)


def _series(frame: pd.DataFrame, indicator: str, params: dict[str, int | float]) -> np.ndarray:
    column = column_for(indicator, params)
    if column not in frame.columns:
        raise KeyError(f"indicator column {column} is not in the frame")
    return frame[column].to_numpy(dtype=float)
