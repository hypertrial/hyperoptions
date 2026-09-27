"""Unpublished quote-conditioned physical forecast for prospective comparison."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from math import exp, isfinite, log, sqrt

import numpy as np
import polars as pl

from options_api.market_calendar import _calendar
from options_api.market_watch import UnderlyingQuote
from stocksweeper.forecast.market import CacheIntegrityError, clean_completed, price_hash
from stocksweeper.forecast.predictive import PredictiveDistribution, PredictiveForecaster

_LOOKBACK = 60
_DECAY = 0.94
_VERSION = "intraday-open-close-ewma60-shadow-v1"


@dataclass(frozen=True)
class IntradayShadow:
    distribution: PredictiveDistribution | None
    reason: str | None
    quote_time: datetime | None
    overnight_variance_share: float | None
    remaining_variance_fraction: float | None


def _variance_shares(frame: pl.DataFrame) -> tuple[float, float] | None:
    recent = frame.tail(_LOOKBACK + 1)
    if recent.height != _LOOKBACK + 1:
        return None
    opens = np.asarray(recent["open"].to_list(), dtype=float)
    closes = np.asarray(recent["close"].to_list(), dtype=float)
    if not np.isfinite(opens).all() or not np.isfinite(closes).all():
        return None
    if np.any(opens <= 0) or np.any(closes <= 0):
        return None
    overnight = np.log(opens[1:] / closes[:-1])
    regular = np.log(closes[1:] / opens[1:])
    weights = _DECAY ** np.arange(_LOOKBACK - 1, -1, -1)
    overnight_var = float(np.average(overnight * overnight, weights=weights))
    regular_var = float(np.average(regular * regular, weights=weights))
    total = overnight_var + regular_var
    if not all(isfinite(x) for x in (overnight_var, regular_var, total)) or total <= 1e-16:
        return None
    return overnight_var / total, regular_var / total


def forecast_intraday_shadow(
    forecaster: PredictiveForecaster,
    completed_close: PredictiveDistribution,
    quote: UnderlyingQuote | None,
    *,
    historical_input: pl.DataFrame | None = None,
) -> IntradayShadow:
    """Use a live quote and verified OHLC cache, or an explicit historical input.

    Historical input is for current-vintage retrospective screening only.
    It must never be labeled an as-issued live forecast.
    """
    if quote is None:
        return IntradayShadow(None, "underlying_quote_unavailable", None, None, None)
    if completed_close.status != "available" or completed_close.spot is None:
        return IntradayShadow(
            None, "completed_close_forecast_unavailable", quote.quote_time, None, None
        )
    calendar = forecaster.calendar
    if (
        quote.session_date != calendar.offset(completed_close.as_of, 1)
        or quote.session_date > completed_close.expiry_session
        or completed_close.horizon_sessions < 1
        or quote.spot <= 0
    ):
        return IntradayShadow(None, "quote_session_mismatch", quote.quote_time, None, None)
    session = _calendar().date_to_session(quote.session_date.isoformat(), direction="none")
    opened = _calendar().session_open(session).to_pydatetime()
    closed = _calendar().session_close(session).to_pydatetime()
    if not opened <= quote.quote_time < closed:
        return IntradayShadow(None, "quote_outside_regular_session", quote.quote_time, None, None)
    if historical_input is not None:
        frame = historical_input
    else:
        try:
            frame = forecaster.prices.read(completed_close.ticker)
        except (CacheIntegrityError, OSError, ValueError):
            frame = None
    if frame is None or frame.is_empty() or frame["ts"][-1] != completed_close.as_of:
        return IntradayShadow(
            None, "verified_open_close_history_unavailable", quote.quote_time, None, None
        )
    clean = clean_completed(frame, completed_close.as_of, calendar)
    if completed_close.data_hash is None or price_hash(clean) != completed_close.data_hash:
        return IntradayShadow(None, "completed_close_input_revised", quote.quote_time, None, None)
    expected = (
        calendar.sessions(clean["ts"][max(0, clean.height - 61)], completed_close.as_of)
        if not clean.is_empty()
        else ()
    )
    if clean.height < 61 or clean.tail(61)["ts"].to_list() != list(expected):
        return IntradayShadow(None, "open_close_history_has_gaps", quote.quote_time, None, None)
    shares = _variance_shares(clean)
    if shares is None:
        return IntradayShadow(None, "open_close_variance_unusable", quote.quote_time, None, None)
    overnight_share, regular_share = shares
    session_remaining = (closed - quote.quote_time).total_seconds() / (
        closed - opened
    ).total_seconds()
    horizon = completed_close.horizon_sessions
    # After a live quote, today's overnight move has happened. Every later
    # session still has its full overnight and regular-session risk.
    remaining = ((horizon - 1) + regular_share * session_remaining) / horizon
    if not 0 < remaining <= 1:
        return IntradayShadow(None, "remaining_variance_unusable", quote.quote_time, None, None)
    scale = sqrt(remaining)
    anchor = float(quote.spot)
    prices = tuple(
        anchor * exp(log(value / completed_close.spot) * scale) for value in completed_close.prices
    )
    if not prices or any(not isfinite(value) or value <= 0 for value in prices):
        return IntradayShadow(None, "intraday_scenario_invalid", quote.quote_time, None, None)
    basis = json.dumps(
        {
            "completed_hash": completed_close.data_hash,
            "quote_source": quote.source,
            "quote_fetched_at": quote.fetched_at.isoformat(),
            "quote_time": quote.quote_time.isoformat(),
            "quote_spot": str(quote.spot),
            "remaining": remaining,
        },
        sort_keys=True,
    ).encode()
    distribution = replace(
        completed_close,
        method="intraday_shadow",
        spot=anchor,
        model_version=_VERSION,
        data_hash=hashlib.sha256(basis).hexdigest(),
        terminal_prices=prices,
    )
    return IntradayShadow(distribution, None, quote.quote_time, overnight_share, remaining)
