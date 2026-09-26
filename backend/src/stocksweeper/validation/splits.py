"""Chronological train, validation, and test splits.

Short histories use a wider out-of-sample fraction and are flagged. The test
segment is never an input to ranking.
"""

from __future__ import annotations

from stocksweeper.config import ValidationSettings

SEGMENTS = ("train", "validation", "test")


def split_segments(
    n_bars: int, settings: ValidationSettings
) -> tuple[bool, dict[str, tuple[int, int]]]:
    limited = n_bars < settings.limited_history_bars
    if limited:
        fractions = (settings.limited_train, settings.limited_validation, settings.limited_test)
    else:
        fractions = (settings.train, settings.validation, settings.test)
    bounds: dict[str, tuple[int, int]] = {}
    start = 0
    for index, name in enumerate(SEGMENTS):
        if index == len(SEGMENTS) - 1:
            end = n_bars
        else:
            end = min(n_bars, start + int(n_bars * fractions[index]))
        bounds[name] = (start, end)
        start = end
    return limited, bounds
