"""Indicative option moneyness from dated, unadjusted-request Yahoo Close."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from collections.abc import Callable
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from options_api.market_calendar import (
    first_session_after_completed,
    latest_completed_session,
    session_on_or_before,
)

_NY = ZoneInfo("America/New_York")
TERMS_NOTE = "Assuming standard 100-share terms."
SOURCE = "Yahoo Finance daily Close (auto_adjust=False; split-adjusted)"
MAX_OUTCOME_HISTORY_DAYS = 5 * 366
MAX_CLOSE_ROWS = 2_000


def _finite_action(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class CloseHistory:
    closes: dict[date, Decimal]
    split_dates: frozenset[date]
    ambiguous_action_dates: frozenset[date]
    actions_verified: bool


class CloseProvider(Protocol):
    def fetch(self, ticker: str, start: date, end: date) -> CloseHistory: ...


class YahooCloseProvider:
    def fetch(self, ticker: str, start: date, end: date) -> CloseHistory:
        import yfinance as yf

        frame = yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=end.isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=True,
            repair=False,
            timeout=20,
            raise_errors=True,
        )
        if frame is None or frame.empty:
            return CloseHistory({}, frozenset(), frozenset(), True)
        if len(frame) > MAX_CLOSE_ROWS:
            raise ValueError("Yahoo close history exceeds the bounded row limit")
        if "Close" not in frame or "Stock Splits" not in frame:
            return CloseHistory({}, frozenset(), frozenset(), False)
        closes: dict[date, Decimal] = {}
        splits: set[date] = set()
        ambiguous: set[date] = set()
        actions_verified = True
        for timestamp, row in frame.iterrows():
            day = timestamp.date()
            if day in closes:
                raise ValueError("duplicate Yahoo daily session")
            raw_close = row["Close"]
            if math.isfinite(float(raw_close)) and float(raw_close) > 0:
                closes[day] = Decimal(str(raw_close))
            raw_split = row["Stock Splits"]
            split = _finite_action(raw_split)
            if split is None:
                actions_verified = False
            elif split != 0:
                splits.add(day)
            if "Capital Gains" in frame:
                raw_action = row["Capital Gains"]
                action = _finite_action(raw_action)
                if action is None:
                    actions_verified = False
                elif action != 0:
                    ambiguous.add(day)
        return CloseHistory(closes, frozenset(splits), frozenset(ambiguous), actions_verified)


@dataclass(frozen=True)
class OutcomeResult:
    status: Literal["pending", "provisional", "unsupported"]
    classification: Literal["itm", "atm", "otm"] | None
    reason: str | None
    source: str | None
    session_date: date | None
    retrieved_at: datetime
    close_price: Decimal | None
    terms_note: str = TERMS_NOTE
    preserve_prior: bool = False


def classify(
    side: Literal["call", "put"], close: Decimal, strike: Decimal
) -> Literal["itm", "atm", "otm"]:
    if close == strike:
        return "atm"
    if side == "call":
        return "itm" if close > strike else "otm"
    return "itm" if close < strike else "otm"


def resolve_outcome(
    *,
    ticker: str,
    root: str,
    side: Literal["call", "put"],
    strike: Decimal,
    expiration: date,
    watched_at: datetime,
    as_of: datetime,
    provider: CloseProvider,
    clock: Callable[[], datetime] | None = None,
) -> OutcomeResult:
    """Use the last scheduled session on/before expiry, never an earlier missing bar."""
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    if watched_at.tzinfo is None:
        watched_at = watched_at.replace(tzinfo=UTC)
    target = session_on_or_before(expiration)
    if root != ticker:
        return OutcomeResult(
            "unsupported", None, "Option root is adjusted or ambiguous", None, target, as_of, None
        )
    if latest_completed_session(as_of) < target:
        return OutcomeResult(
            "pending", None, "Expiry trading session has not completed", None, target, as_of, None
        )
    start = min(watched_at.astimezone(_NY).date(), target)
    action_start = first_session_after_completed(watched_at)
    end = as_of.astimezone(_NY).date() + timedelta(days=1)
    if (end - start).days > MAX_OUTCOME_HISTORY_DAYS:
        return OutcomeResult(
            "unsupported",
            None,
            "Contract is too old to verify a bounded corporate-action history",
            SOURCE,
            target,
            as_of,
            None,
            preserve_prior=True,
        )
    try:
        history = provider.fetch(ticker, start, end)
    except Exception:
        return OutcomeResult(
            "pending", None, "Yahoo close is temporarily unavailable", SOURCE, target, as_of, None
        )
    retrieved_at = clock() if clock is not None else datetime.now(UTC)
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=UTC)
    if not history.actions_verified:
        return OutcomeResult(
            "pending",
            None,
            "Corporate-action history is unavailable; retry later",
            SOURCE,
            target,
            retrieved_at,
            None,
        )
    if any(action_start <= day <= target for day in history.split_dates):
        return OutcomeResult(
            "unsupported",
            None,
            "Stock split may have changed contract terms or Yahoo Close",
            SOURCE,
            target,
            retrieved_at,
            None,
        )
    if any(action_start <= day <= target for day in history.ambiguous_action_dates):
        return OutcomeResult(
            "unsupported",
            None,
            "Corporate action may have changed contract terms",
            SOURCE,
            target,
            retrieved_at,
            None,
        )
    if any(target < day <= as_of.astimezone(_NY).date() for day in history.split_dates):
        return OutcomeResult(
            "unsupported",
            None,
            "A post-expiry stock split prevents a safe Yahoo Close recheck",
            SOURCE,
            target,
            retrieved_at,
            None,
            preserve_prior=True,
        )
    if any(target < day <= as_of.astimezone(_NY).date() for day in history.ambiguous_action_dates):
        return OutcomeResult(
            "unsupported",
            None,
            "A post-expiry corporate action prevents a safe Yahoo Close recheck",
            SOURCE,
            target,
            retrieved_at,
            None,
            preserve_prior=True,
        )
    close = history.closes.get(target)
    if close is None:
        return OutcomeResult(
            "pending",
            None,
            "Yahoo has not supplied the expiry-session Close",
            SOURCE,
            target,
            retrieved_at,
            None,
        )
    return OutcomeResult(
        "provisional", classify(side, close, strike), None, SOURCE, target, retrieved_at, close
    )
