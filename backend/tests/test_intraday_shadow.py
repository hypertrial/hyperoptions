from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import exp
from types import SimpleNamespace

import polars as pl

from options_api.intraday_shadow import forecast_intraday_shadow
from options_api.market_calendar import session_close
from options_api.market_watch import UnderlyingQuote
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import price_hash
from stocksweeper.forecast.predictive import PredictiveDistribution


def _history(calendar: SessionCalendar, completed: date) -> pl.DataFrame:
    days = calendar.sessions(calendar.offset(completed, -60), completed)
    close = 100.0
    rows = []
    for index, day in enumerate(days):
        overnight = 0.02 if index % 2 else -0.02
        regular = 0.01 if index % 3 else -0.01
        opened = close * exp(overnight)
        close = opened * exp(regular)
        rows.append((day, opened, max(opened, close), min(opened, close), close, 1000.0, 0.0, 0.0))
    return pl.DataFrame(
        rows,
        schema=["ts", "open", "high", "low", "close", "volume", "dividends", "stock_splits"],
        orient="row",
    ).with_columns(pl.col("ts").cast(pl.Date))


def test_intraday_shadow_keeps_future_overnights_and_excludes_elapsed_first_overnight() -> None:
    completed = date(2026, 9, 25)
    expiry = date(2026, 10, 2)
    calendar = SessionCalendar()
    frame = _history(calendar, completed)
    forecaster = SimpleNamespace(
        calendar=calendar,
        prices=SimpleNamespace(read=lambda _ticker: frame),
    )
    distribution = PredictiveDistribution(
        ticker="TEST",
        status="available",
        reason=None,
        method="empirical_scaled",
        as_of=completed,
        expiry_session=expiry,
        horizon_sessions=5,
        spot=100.0,
        daily_volatility=0.02,
        model_version="test",
        support=3,
        data_hash=price_hash(frame),
        terminal_prices=(80.0, 100.0, 120.0),
        weights=(0.2, 0.5, 0.3),
    )
    monday_open = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
    quote = UnderlyingQuote(
        spot=Decimal("103"),
        session_date=date(2026, 9, 28),
        source="nasdaq",
        fetched_at=monday_open,
        quote_time=monday_open,
    )
    open_result = forecast_intraday_shadow(forecaster, distribution, quote)
    assert open_result.distribution is not None, open_result.reason
    assert 0 < open_result.overnight_variance_share < 1
    assert 0.8 < open_result.remaining_variance_fraction < 1
    assert open_result.distribution.spot == 103
    assert open_result.distribution.probability("call", Decimal("103")) == 0.3
    midday = quote.__class__(
        spot=quote.spot,
        session_date=quote.session_date,
        source=quote.source,
        fetched_at=datetime(2026, 9, 28, 17, tzinfo=UTC),
        quote_time=datetime(2026, 9, 28, 17, tzinfo=UTC),
    )
    midday_result = forecast_intraday_shadow(forecaster, distribution, midday)
    assert midday_result.distribution is not None
    assert midday_result.remaining_variance_fraction < open_result.remaining_variance_fraction
    assert midday_result.remaining_variance_fraction > 0.8  # Four future full sessions.

    same_day = replace(distribution, expiry_session=quote.session_date, horizon_sessions=1)
    late_time = session_close(quote.session_date) - timedelta(minutes=30)
    late_quote = replace(quote, fetched_at=late_time, quote_time=late_time)
    late_result = forecast_intraday_shadow(forecaster, same_day, late_quote)
    assert late_result.distribution is not None
    assert 0 < late_result.remaining_variance_fraction < 0.1


def test_intraday_shadow_rejects_missing_or_mismatched_quote() -> None:
    calendar = SessionCalendar()
    completed = date(2026, 9, 25)
    distribution = PredictiveDistribution(
        ticker="TEST",
        status="available",
        reason=None,
        method="empirical_scaled",
        as_of=completed,
        expiry_session=date(2026, 9, 28),
        horizon_sessions=1,
        spot=100.0,
        daily_volatility=0.02,
        terminal_prices=(80.0, 120.0),
        weights=(0.5, 0.5),
    )
    forecaster = SimpleNamespace(calendar=calendar, prices=SimpleNamespace(read=lambda _: None))
    assert forecast_intraday_shadow(forecaster, distribution, None).reason == (
        "underlying_quote_unavailable"
    )
    stale_session = UnderlyingQuote(
        Decimal("101"),
        completed,
        "nasdaq",
        datetime(2026, 9, 25, 18, tzinfo=UTC),
        datetime(2026, 9, 25, 18, tzinfo=UTC),
    )
    assert forecast_intraday_shadow(forecaster, distribution, stale_session).reason == (
        "quote_session_mismatch"
    )
