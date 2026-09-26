"""Robustness score. Validation and walk-forward only; the test segment is absent."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from stocksweeper.backtest.metrics import Metrics
from stocksweeper.config import GateSettings, RobustnessWeights

GATE_FLAGS = frozenset(
    {
        "insufficient_trades",
        "extreme_drawdown",
        "severe_degradation",
        "unstable_parameters",
    }
)


@dataclass(frozen=True)
class Robustness:
    score: float
    sharpe_component: float
    cagr_component: float
    drawdown_component: float
    profit_factor_component: float
    trade_component: float
    walk_forward_component: float
    stability_component: float
    degradation: float
    flags: list[str]
    rejected: bool


def score_strategy(
    train: Metrics,
    validation: Metrics,
    *,
    consistency: float,
    stability: float,
    weights: RobustnessWeights,
    gates: GateSettings,
    neighbours: int = 1,
) -> Robustness:
    sharpe_component = _unit_sharpe(validation.sharpe)
    cagr_component = _unit_cagr(validation.cagr)
    drawdown_component = _unit_drawdown(validation.max_drawdown)
    profit_factor_component = _unit_profit_factor(validation.profit_factor, validation.n_trades)
    trade_component = _unit_trades(validation.n_trades)
    walk_forward_component = float(np.clip(consistency, 0.0, 1.0))
    stability_component = float(np.clip(stability, 0.0, 1.0))
    weighted = (
        weights.sharpe * sharpe_component
        + weights.cagr * cagr_component
        + weights.max_drawdown * drawdown_component
        + weights.profit_factor * profit_factor_component
        + weights.trade_sufficiency * trade_component
        + weights.walk_forward * walk_forward_component
        + weights.stability * stability_component
    )
    degradation = _degradation(train.sharpe, validation.sharpe)
    flags: list[str] = []
    trade_count = train.n_trades + validation.n_trades
    if trade_count < gates.min_trades or validation.n_trades < gates.min_val_trades:
        flags.append("insufficient_trades")
    if validation.max_drawdown is not None and validation.max_drawdown < -gates.max_drawdown:
        flags.append("extreme_drawdown")
    if degradation < gates.min_degradation:
        flags.append("severe_degradation")
    if stability_component < gates.min_stability:
        flags.append("unstable_parameters")
    if neighbours == 0:
        flags.append("no_neighbours")
    return Robustness(
        score=float(100.0 * weighted * degradation),
        sharpe_component=sharpe_component,
        cagr_component=cagr_component,
        drawdown_component=drawdown_component,
        profit_factor_component=profit_factor_component,
        trade_component=trade_component,
        walk_forward_component=walk_forward_component,
        stability_component=stability_component,
        degradation=degradation,
        flags=flags,
        rejected=any(flag in GATE_FLAGS for flag in flags),
    )


def cross_ticker_score(scores: list[float], penalty_k: float) -> float:
    if not scores:
        return 0.0
    return float(np.mean(scores) - penalty_k * np.std(scores))


def _unit_sharpe(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    return float(np.clip(value / 2.0, 0.0, 1.0))


def _unit_cagr(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    return float(np.clip(value / 0.5, 0.0, 1.0))


def _unit_drawdown(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    return float(np.clip(1.0 - abs(value) / 0.6, 0.0, 1.0))


def _unit_profit_factor(value: float | None, n_trades: int) -> float:
    if value is None:
        return 1.0 if n_trades > 0 else 0.0
    if not math.isfinite(value):
        return 1.0
    return float(np.clip((value - 1.0) / 2.0, 0.0, 1.0))


def _unit_trades(n_trades: int, target: float = 30.0, scale: float = 8.0) -> float:
    return float(1.0 / (1.0 + math.exp(-(n_trades - target) / scale)))


def _degradation(in_sample: float | None, out_of_sample: float | None) -> float:
    if in_sample is None or out_of_sample is None:
        return 0.0
    if not math.isfinite(in_sample) or not math.isfinite(out_of_sample):
        return 0.0
    if in_sample <= 0:
        return 1.0 if out_of_sample >= in_sample else 0.0
    return float(np.clip(out_of_sample / in_sample, 0.0, 1.0))
