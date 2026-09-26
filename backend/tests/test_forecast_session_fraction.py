"""Remaining physical forecast variance follows trading sessions, not weekends."""

from datetime import UTC, date, datetime
from decimal import Decimal

from options_api.market_calendar import remaining_session_variance_fraction


def test_weekend_does_not_consume_a_friday_forecast_horizon() -> None:
    completed = date(2026, 9, 25)
    expiry = date(2026, 10, 2)
    monday_before_open = datetime(2026, 9, 28, 13, tzinfo=UTC)

    assert remaining_session_variance_fraction(
        completed, expiry, monday_before_open
    ) == Decimal(1)

    monday_midmorning = datetime(2026, 9, 28, 15, tzinfo=UTC)
    remaining = remaining_session_variance_fraction(completed, expiry, monday_midmorning)
    assert remaining is not None
    assert abs(float(remaining) - (5 * 390 - 90) / (5 * 390)) < 1e-9


def test_early_close_counts_only_scheduled_regular_minutes() -> None:
    # Thanksgiving is closed; the following Friday has a 13:00 ET close.
    completed = date(2026, 11, 25)
    expiry = date(2026, 11, 27)
    friday_at_eleven_et = datetime(2026, 11, 27, 16, tzinfo=UTC)

    remaining = remaining_session_variance_fraction(
        completed, expiry, friday_at_eleven_et
    )
    assert remaining is not None
    assert abs(float(remaining) - 120 / 210) < 1e-9

    assert remaining_session_variance_fraction(
        completed, expiry, datetime(2026, 11, 27, 18, tzinfo=UTC)
    ) is None
