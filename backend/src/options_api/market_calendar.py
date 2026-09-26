"""Completed US equity sessions for option research and indicative outcomes."""

from __future__ import annotations

from datetime import UTC, date, datetime
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
