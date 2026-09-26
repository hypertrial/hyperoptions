"""Parameter stability from adjacent points on the same grid."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from stocksweeper.strategy.model import Strategy
from stocksweeper.strategy.primitives import parameter_axes


@dataclass(frozen=True)
class Stability:
    score: float
    neighbours: int


def stability_scores(
    strategies: list[Strategy],
    sharpe_by_id: dict[str, float | None],
) -> dict[str, Stability]:
    axes = parameter_axes()
    index = {
        (strategy.family, _freeze(strategy.parameters)): strategy.id for strategy in strategies
    }
    scores: dict[str, Stability] = {}
    for strategy in strategies:
        neighbour_ids: list[str] = []
        for key, value in strategy.parameters.items():
            axis = axes.get(key, [])
            if value not in axis:
                continue
            position = axis.index(value)
            for step in (position - 1, position + 1):
                if step < 0 or step >= len(axis):
                    continue
                altered = dict(strategy.parameters)
                altered[key] = axis[step]
                match = index.get((strategy.family, _freeze(altered)))
                if match is not None and match != strategy.id:
                    neighbour_ids.append(match)
        neighbour_sharpes = [sharpe_by_id.get(item) for item in neighbour_ids]
        score, count = _stability(sharpe_by_id.get(strategy.id), neighbour_sharpes)
        scores[strategy.id] = Stability(score, count)
    return scores


def _stability(own: float | None, neighbours: list[float | None]) -> tuple[float, int]:
    finite = [value for value in neighbours if value is not None and math.isfinite(value)]
    if not finite:
        return 0.5, 0
    # A zero or missing own Sharpe makes the neighbour ratio undefined.
    if own is None or not math.isfinite(own) or own == 0:
        return 0.0, len(finite)
    ratio = float(np.mean(finite) / own)
    return float(np.clip(ratio, 0.0, 1.0)), len(finite)


def _freeze(parameters: dict[str, int | float]) -> tuple[tuple[str, int | float], ...]:
    return tuple(sorted(parameters.items()))
