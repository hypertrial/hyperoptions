"""Close-based exits. A stop is a signal at the close and fills on the next open.

The engine shifts every signal by one bar, so this module stays in signal time.
While flat, an entry is taken only when the exit is false. While long, an entry
is ignored and an exit closes. That is VectorBT's ``accumulate=False`` with
``upon_long_conflict="exit"``. A position is not opened on the last bar, because
that fill does not exist.
"""

from __future__ import annotations

import numpy as np
from numba import njit

from stocksweeper.strategy.model import ExitRule

TIME_BARS = 10
ATR_LENGTH = 14
ATR_MULT = 3.0

KIND_TIME = 1
KIND_ATR = 2

_CLAIMS = frozenset({"trend", "pullback", "fade", "continuation", "breakout"})
_MEAN_REVERSION = frozenset({"pullback", "fade"})


def menu_for(claim: str) -> tuple[ExitRule, ...]:
    """Mirror, plus the one stop that matches the signal's claim."""
    if claim not in _CLAIMS:
        raise ValueError(f"unknown claim {claim}")
    mirror = ExitRule()
    if claim in _MEAN_REVERSION:
        return (mirror, ExitRule(kind="time", bars=TIME_BARS))
    return (mirror, ExitRule(kind="atr_trail", atr_length=ATR_LENGTH, atr_mult=ATR_MULT))


def exit_label(rule: ExitRule) -> str:
    if rule.kind == "time":
        return f"Time stop {rule.bars} bars"
    if rule.kind == "atr_trail":
        return f"ATR({rule.atr_length}) trail {rule.atr_mult:g}x"
    return ""


def apply_exit(
    entries: np.ndarray,
    exits: np.ndarray,
    close: np.ndarray,
    atr: np.ndarray,
    rule: ExitRule,
) -> tuple[np.ndarray, np.ndarray]:
    """Return entry and exit signals with the stop applied. Mirror is unchanged."""
    if rule.kind == "mirror":
        return entries, exits
    kind = KIND_TIME if rule.kind == "time" else KIND_ATR
    return _stop_signals(
        np.asarray(entries, dtype=np.bool_),
        np.asarray(exits, dtype=np.bool_),
        np.asarray(close, dtype=np.float64),
        np.asarray(atr, dtype=np.float64),
        kind,
        int(rule.bars),
        float(rule.atr_mult),
    )


@njit(cache=True)
def _stop_signals(
    entries: np.ndarray,
    exits: np.ndarray,
    close: np.ndarray,
    atr: np.ndarray,
    kind: int,
    bars: int,
    mult: float,
) -> tuple[np.ndarray, np.ndarray]:
    count = len(entries)
    entry_out = np.zeros(count, dtype=np.bool_)
    exit_out = np.zeros(count, dtype=np.bool_)
    in_position = False
    signal_bar = 0
    peak = -np.inf
    for bar in range(count):
        if in_position:
            fill = signal_bar + 1
            if bar >= fill and np.isfinite(close[bar]) and close[bar] > peak:
                peak = close[bar]
            stop = False
            if kind == KIND_TIME and bar - signal_bar == bars:
                stop = True
            elif (
                kind == KIND_ATR
                and bar >= fill
                and np.isfinite(close[bar])
                and close[bar] < peak - mult * atr[signal_bar]
            ):
                stop = True
            if exits[bar] or stop:
                exit_out[bar] = True
                in_position = False
            continue
        if entries[bar] and not exits[bar] and bar + 1 < count:
            if kind == KIND_ATR and not np.isfinite(atr[bar]):
                continue
            entry_out[bar] = True
            in_position = True
            signal_bar = bar
            peak = -np.inf
    return entry_out, exit_out
