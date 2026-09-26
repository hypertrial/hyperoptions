"""Rolling walk-forward over the non-test region only."""

from __future__ import annotations

import math

import numpy as np

from stocksweeper.backtest.metrics import sharpe_matrix


def fold_windows(n_region: int, folds: int) -> list[tuple[int, int, int, int]]:
    """Return (is_start, is_end, oos_start, oos_end) for each fold.

    The in-sample window is everything before the out-of-sample slice. The
    out-of-sample length is ``region / (folds + 1)``. Bars that do not fill a
    final slice are left unused. The test region is not included.
    """
    if folds < 1 or n_region < folds + 1:
        return []
    width = n_region // (folds + 1)
    if width < 1:
        return []
    windows: list[tuple[int, int, int, int]] = []
    for fold in range(folds):
        is_end = (fold + 1) * width
        oos_end = (fold + 2) * width
        if oos_end > n_region:
            break
        windows.append((0, is_end, is_end, oos_end))
    return windows


def fold_metrics(
    returns: np.ndarray,
    windows: list[tuple[int, int, int, int]],
    periods_per_year: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """In-sample Sharpe, out-of-sample Sharpe, and out-of-sample total return."""
    count = returns.shape[1] if returns.ndim == 2 else 0
    is_sharpe = np.full((len(windows), count), np.nan)
    oos_sharpe = np.full((len(windows), count), np.nan)
    oos_return = np.full((len(windows), count), np.nan)
    for index, (_, is_end, oos_start, oos_end) in enumerate(windows):
        is_sharpe[index] = sharpe_matrix(returns[:is_end], periods_per_year)
        oos_slice = returns[oos_start:oos_end]
        oos_sharpe[index] = sharpe_matrix(oos_slice, periods_per_year)
        if len(oos_slice):
            oos_return[index] = np.prod(1.0 + np.nan_to_num(oos_slice, nan=0.0), axis=0) - 1.0
    return is_sharpe, oos_sharpe, oos_return


def consistency(oos_sharpes: np.ndarray) -> float:
    values = oos_sharpes[np.isfinite(oos_sharpes)]
    if len(values) == 0:
        return 0.0
    positive = float(np.mean(values > 0))
    dispersion = 1.0 / (1.0 + float(np.std(values)))
    score = 0.5 * positive + 0.5 * dispersion
    if not math.isfinite(score):
        return 0.0
    return float(score)


def reoptimized(
    is_sharpe: np.ndarray,
    oos_sharpe: np.ndarray,
    oos_return: np.ndarray,
    groups: dict[str, list[int]],
) -> dict[str, list[tuple[int, int, float | None, float | None]]]:
    """Per family, the parameter set with the best in-sample Sharpe each fold."""
    picked: dict[str, list[tuple[int, int, float | None, float | None]]] = {}
    for family, columns in groups.items():
        rows: list[tuple[int, int, float | None, float | None]] = []
        if not columns:
            picked[family] = rows
            continue
        for fold in range(is_sharpe.shape[0]):
            scores = is_sharpe[fold, columns]
            if not np.isfinite(scores).any():
                continue
            best_offset = int(np.nanargmax(scores))
            column = columns[best_offset]
            rows.append(
                (
                    fold + 1,
                    column,
                    _maybe(oos_sharpe[fold, column]),
                    _maybe(oos_return[fold, column]),
                )
            )
        picked[family] = rows
    return picked


def _maybe(value: float) -> float | None:
    if not math.isfinite(float(value)):
        return None
    return float(value)
