"""Completed US equity sessions for watch forecasts and indicative outcomes."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars

_NY = ZoneInfo("America/New_York")


@lru_cache(maxsize=1)
def _calendar():
    return exchange_calendars.get_calendar(
        "XNYS", start="2000-01-01", end=f"{date.today().year + 5}-12-31"
    )


def session_on_or_before(day: date) -> date:
    return _calendar().date_to_session(day.isoformat(), direction="previous").date()


def session_close(day: date) -> datetime:
    session = _calendar().date_to_session(day.isoformat(), direction="none")
    return _calendar().session_close(session).to_pydatetime()


def regular_session_open(as_of: datetime) -> bool:
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    return bool(_calendar().is_open_on_minute(as_of))


def quote_session(as_of: datetime) -> date:
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    if regular_session_open(as_of):
        return as_of.astimezone(_NY).date()
    return latest_completed_session(as_of)


def latest_completed_session(as_of: datetime) -> date:
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    local_date = as_of.astimezone(_NY).date()
    session = _calendar().date_to_session(local_date.isoformat(), direction="previous")
    if as_of < _calendar().session_close(session).to_pydatetime():
        session = _calendar().previous_session(session)
    return session.date()


def expiry_session_completed(expiration: date, as_of: datetime) -> bool:
    return latest_completed_session(as_of) >= session_on_or_before(expiration)


@lru_cache(maxsize=8192)
def remaining_session_variance_fraction(
    completed: date, expiry: date, quote_time: datetime
) -> Decimal | None:
    """Fraction of future regular-session minutes remaining after a quote.

    A forecast horizon is measured in exchange sessions, so weekends and
    overnight hours consume no forecast variance. Early closes contribute
    only their scheduled trading minutes.
    """
    if quote_time.tzinfo is None:
        quote_time = quote_time.replace(tzinfo=UTC)
    last = session_on_or_before(expiry)
    if last <= completed:
        return None
    calendar = _calendar()
    first_session = calendar.date_to_session(completed.isoformat(), direction="none")
    last_session = calendar.date_to_session(last.isoformat(), direction="none")
    sessions = calendar.sessions_in_range(first_session, last_session)[1:]
    start = calendar.session_close(first_session).to_pydatetime()
    end = calendar.session_close(last_session).to_pydatetime()
    if quote_time < start or quote_time >= end:
        return None
    total = 0.0
    remaining = 0.0
    for session in sessions:
        opened = calendar.session_open(session).to_pydatetime()
        closed = calendar.session_close(session).to_pydatetime()
        total += (closed - opened).total_seconds()
        if quote_time < closed:
            remaining += (closed - max(quote_time, opened)).total_seconds()
    if total <= 0 or remaining <= 0:
        return None
    return Decimal(str(remaining / total))
