"""Completed regular equity sessions, shared by signal and outcome horizons."""

from __future__ import annotations

from datetime import date, datetime

import exchange_calendars as xcals

from options_api.market_calendar import latest_completed_session, session_on_or_before

class SessionCalendar:
    def __init__(self) -> None:
        self.exchange = xcals.get_calendar(
            "XNYS", start="1970-01-01", end=f"{date.today().year + 5}-12-31"
        )

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        if end < start:
            return ()
        return tuple(item.date() for item in self.exchange.sessions_in_range(start, end))

    def last_completed(self, as_of: datetime) -> date:
        if as_of.tzinfo is None:
            raise ValueError("as_of must have a timezone")
        return latest_completed_session(as_of)

    def expiry_session(self, expiry: date) -> date:
        return session_on_or_before(expiry)

    def offset(self, session: date, sessions: int) -> date:
        return self.exchange.session_offset(session, sessions).date()

    def horizon(self, as_of: date, expiry: date) -> int:
        last = self.expiry_session(expiry)
        if last <= as_of:
            return 0
        return len(self.sessions(as_of, last)) - 1
