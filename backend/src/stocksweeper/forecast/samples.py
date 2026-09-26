"""One matured, scheduled-session return per ticker/date/horizon."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from math import log, sqrt

import numpy as np
import polars as pl

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.calibration import Observation
from stocksweeper.forecast.models import State

VOLATILITY_LOOKBACK = 20
MAX_HORIZON = 25


def standardized_strike(strike: Decimal, spot: float, volatility: float, horizon: int) -> float:
    """Put strike and matured peer returns on the same as-of volatility scale."""
    if strike <= 0 or spot <= 0 or volatility <= 0 or horizon < 1:
        raise ValueError("positive strike, spot, volatility and horizon are required")
    return log(float(strike) / spot) / (volatility * sqrt(horizon))


def _aligned(
    bars: pl.DataFrame, states: tuple[State, ...], calendar: SessionCalendar
) -> tuple[tuple[date, ...], np.ndarray, np.ndarray, list[State | None], np.ndarray]:
    if len(states) != bars.height:
        raise ValueError("one causal signal state is required per bar")
    sessions = calendar.sessions(bars["ts"][0], bars["ts"][-1])
    index = {day: position for position, day in enumerate(sessions)}
    closes = np.full(len(sessions), np.nan)
    splits = np.zeros(len(sessions), dtype=bool)
    state_by_session: list[State | None] = [None] * len(sessions)
    for row, state in zip(bars.iter_rows(named=True), states, strict=True):
        position = index.get(row["ts"])
        if position is None:
            continue
        closes[position] = float(row["close"])
        splits[position] = float(row["stock_splits"]) > 0
        state_by_session[position] = state
    daily = np.full(len(sessions), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        daily[1:] = np.log(closes[1:] / closes[:-1])
    volatility = np.full(len(sessions), np.nan)
    for position in range(VOLATILITY_LOOKBACK, len(sessions)):
        trailing = daily[position - VOLATILITY_LOOKBACK + 1 : position + 1]
        if (
            np.isfinite(trailing).all()
            and not splits[position - VOLATILITY_LOOKBACK + 1 : position + 1].any()
        ):
            value = np.std(trailing, ddof=1)
            if value > 1e-6:
                volatility[position] = value
    return sessions, closes, volatility, state_by_session, np.cumsum(splits.astype(int))


def observations(
    ticker: str,
    bars: pl.DataFrame,
    states: tuple[State, ...],
    horizon: int,
    start: date,
    end: date,
    calendar: SessionCalendar,
) -> list[Observation]:
    if horizon < 1 or horizon > MAX_HORIZON or bars.is_empty():
        return []
    sessions, closes, volatility, state_by_session, split_count = _aligned(bars, states, calendar)
    result = []
    for position, day in enumerate(sessions):
        maturity_position = position + horizon
        if maturity_position >= len(sessions):
            break
        maturity = sessions[maturity_position]
        if day < start or maturity > end:
            continue
        state = state_by_session[position]
        if state is None or not np.isfinite(volatility[position]):
            continue
        if not np.isfinite(closes[maturity_position]):
            continue
        if split_count[maturity_position] != split_count[position]:
            continue
        standardized = log(closes[maturity_position] / closes[position]) / (
            volatility[position] * sqrt(horizon)
        )
        if np.isfinite(standardized):
            result.append(Observation(ticker, day, maturity, horizon, state, float(standardized)))
    return result


def current_state_and_volatility(
    bars: pl.DataFrame, states: tuple[State, ...], completed: date, calendar: SessionCalendar
) -> tuple[State, float, float] | None:
    if bars.is_empty() or bars["ts"][-1] != completed:
        return None
    sessions, closes, volatility, state_by_session, _ = _aligned(bars, states, calendar)
    if sessions[-1] != completed or state_by_session[-1] is None:
        return None
    if not np.isfinite(volatility[-1]) or not np.isfinite(closes[-1]):
        return None
    return state_by_session[-1], float(volatility[-1]), float(closes[-1])
