from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import exp
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl

from options_api.contract_identity import make_watch_key
from options_api.intraday_capture import capture_intraday_window
from options_api.market_calendar import _calendar
from options_api.market_watch import UnderlyingQuote
from options_api.outcomes import TERMS_NOTE
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.ledger import ForecastIssuance, ForecastLedger
from stocksweeper.forecast.predictive import PredictiveForecaster

_NY = ZoneInfo("America/New_York")


class _Prices:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame

    def fetch(self, _ticker: str, _start: date | None, _end: date) -> pl.DataFrame:
        return self.frame


def _history(calendar: SessionCalendar, completed: date) -> pl.DataFrame:
    days = calendar.sessions(calendar.offset(completed, -60), completed)
    close = 100.0
    rows = []
    for index, day in enumerate(days):
        opened = close * exp(0.02 if index % 2 else -0.02)
        close = opened * exp(0.01 if index % 3 else -0.01)
        rows.append((day, opened, max(opened, close), min(opened, close), close, 1000.0, 0.0, 0.0))
    return pl.DataFrame(
        rows,
        schema=["ts", "open", "high", "low", "close", "volume", "dividends", "stock_splits"],
        orient="row",
    ).with_columns(pl.col("ts").cast(pl.Date))


def _fixture(tmp_path, day: date, *, window: str = "10:00", horizon: int = 3):
    calendar = SessionCalendar()
    prior = calendar.offset(day, -1)
    expiry = calendar.offset(day, horizon)
    frame = _history(calendar, prior)
    forecaster = PredictiveForecaster(tmp_path, provider=_Prices(frame), calendar=calendar)
    forecaster.prepare("TEST", prior)
    hour, minute = map(int, window.split(":"))
    target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=_NY).astimezone(UTC)
    now = target + timedelta(minutes=1)
    session = _calendar().date_to_session(day.isoformat(), direction="none")
    forecast_at = min(
        now, _calendar().session_close(session).to_pydatetime() - timedelta(minutes=1)
    )
    base = forecaster.forecast("TEST", forecast_at, expiry, contract_since=prior)
    assert base.status == "available", base.reason
    strike = Decimal("100.000")
    call = base.probability("call", strike)
    put = base.probability("put", strike)
    assert call is not None and put is not None
    issue = ForecastIssuance(
        contract_key=make_watch_key("TEST", "TEST", "call", expiry.isoformat(), strike),
        ticker="TEST",
        root="TEST",
        side="call",
        expiration=expiry,
        expiry_session=expiry,
        strike_exact="100.000",
        terms_note=TERMS_NOTE,
        contract_since=prior,
        input_session=prior,
        input_retrieved_at=datetime.now(UTC),
        issued_at=target,
        model_version=base.model_version,
        method=base.method,
        data_hash=base.data_hash,
        distribution_hash=None,
        price_basis="completed_close",
        spot_exact=str(base.spot),
        status="available",
        itm_probability=call,
        otm_probability=put,
        atm_probability=max(0.0, 1 - call - put),
        unavailable_reason=None,
    )
    quote = UnderlyingQuote(
        Decimal("103.000"),
        day,
        "nasdaq",
        target + timedelta(seconds=30),
        target + timedelta(seconds=30),
    )
    ledger = ForecastLedger(tmp_path)
    market = SimpleNamespace(underlying_quote=lambda _ticker: quote)
    return ledger, forecaster, market, (issue, base), quote, now


def _future_session() -> date:
    calendar = SessionCalendar()
    return calendar.offset(calendar.last_completed(datetime.now(UTC)), 2)


def test_prospective_window_records_paired_models_and_restart_is_idempotent(tmp_path) -> None:
    ledger, forecaster, market, pair, quote, now = _fixture(tmp_path, _future_session())
    assert capture_intraday_window(ledger, forecaster, market, [pair], now=now, window="10:00") == 2
    assert (
        capture_intraday_window(
            ForecastLedger(tmp_path), forecaster, market, [pair], now=now, window="10:00"
        )
        == 0
    )
    rows = ledger.evaluation_rows()
    assert len(rows) == 2
    assert {row["method"] for row in rows} == {"intraday_shadow", "quote_reanchored_comparator"}
    assert all(row["status"] == "available" for row in rows)
    assert all(row["quote_time"] == quote.quote_time for row in rows)
    assert all(row["quote_source"] == "nasdaq" for row in rows)
    assert all(row["snapshot_window"] == "10:00" for row in rows)
    assert all(
        abs(row["itm_probability"] + row["otm_probability"] + row["atm_probability"] - 1) < 1e-8
        for row in rows
    )
    assert {row["data_hash"] for row in rows} == {pair[0].data_hash}
    assert len({row["quote_digest"] for row in rows}) == 1
    assert all(row["quote_fetched_at"] == quote.fetched_at for row in rows)
    assert all(row["distribution_hash"] for row in rows)


def test_stale_quote_and_revised_cache_are_recorded_as_unavailable(tmp_path) -> None:
    ledger, forecaster, market, pair, quote, now = _fixture(tmp_path, _future_session())
    stale = UnderlyingQuote(
        quote.spot,
        quote.session_date,
        quote.source,
        now - timedelta(minutes=6),
        now - timedelta(minutes=6),
    )
    market.underlying_quote = lambda _ticker: stale
    assert capture_intraday_window(ledger, forecaster, market, [pair], now=now, window="10:00") == 2
    assert {row["unavailable_reason"] for row in ledger.evaluation_rows()} == {
        "underlying_quote_stale"
    }

    # A changed Parquet without the matching manifest cannot silently reuse
    # the old completed-close input or issue either quote-conditioned model.
    day = quote.session_date
    later = datetime(day.year, day.month, day.day, 13, 1, tzinfo=_NY).astimezone(UTC)
    market.underlying_quote = lambda _ticker: UnderlyingQuote(
        quote.spot, quote.session_date, quote.source, later, later
    )
    path = forecaster.prices.path("TEST")
    frame = pl.read_parquet(path)
    frame.with_columns(pl.col("close").last().alias("close")).write_parquet(path)
    assert (
        capture_intraday_window(ledger, forecaster, market, [pair], now=later, window="13:00") == 2
    )
    rows = [row for row in ledger.evaluation_rows() if row["snapshot_window"] == "13:00"]
    assert {row["unavailable_reason"] for row in rows} == {
        "completed_close_input_provenance_unverified"
    }


def test_same_day_expiry_and_missing_ohlc_history_keep_comparator_separate(tmp_path) -> None:
    ledger, forecaster, market, pair, _quote, now = _fixture(
        tmp_path, _future_session(), window="15:30", horizon=0
    )
    forecaster.prices.read = lambda _ticker: None
    assert capture_intraday_window(ledger, forecaster, market, [pair], now=now, window="15:30") == 2
    rows = {row["method"]: row for row in ledger.evaluation_rows()}
    assert rows["quote_reanchored_comparator"]["status"] == "available"
    assert rows["intraday_shadow"]["status"] == "unavailable"
    assert rows["intraday_shadow"]["unavailable_reason"] == (
        "verified_open_close_history_unavailable"
    )
    assert (
        rows["quote_reanchored_comparator"]["quote_digest"]
        == (rows["intraday_shadow"]["quote_digest"])
    )


def test_quote_fetch_preceding_quote_timestamp_is_rejected_without_invalid_ledger_row(
    tmp_path,
) -> None:
    ledger, forecaster, market, pair, quote, now = _fixture(tmp_path, _future_session())
    market.underlying_quote = lambda _ticker: UnderlyingQuote(
        quote.spot,
        quote.session_date,
        quote.source,
        quote.quote_time - timedelta(seconds=1),
        quote.quote_time,
    )
    assert capture_intraday_window(ledger, forecaster, market, [pair], now=now, window="10:00") == 2
    rows = ledger.evaluation_rows()
    assert {row["unavailable_reason"] for row in rows} == {
        "underlying_quote_fetch_precedes_timestamp"
    }
    assert all(row["quote_digest"] is None for row in rows)


def test_early_close_records_rejection_and_holiday_has_no_window(tmp_path) -> None:
    calendar = _calendar()
    completed = SessionCalendar().last_completed(datetime.now(UTC))
    early = next(
        session.date()
        for session in calendar.sessions_in_range(
            calendar.date_to_session(completed.isoformat(), direction="next"),
            calendar.date_to_session(
                (completed + timedelta(days=500)).isoformat(), direction="previous"
            ),
        )
        if (calendar.session_close(session) - calendar.session_open(session)).total_seconds()
        < 6 * 3600
    )
    ledger, forecaster, market, pair, _quote, now = _fixture(tmp_path, early, window="15:30")
    assert capture_intraday_window(ledger, forecaster, market, [pair], now=now, window="15:30") == 2
    assert {row["unavailable_reason"] for row in ledger.evaluation_rows()} == {
        "snapshot_window_outside_regular_session"
    }
    holiday = next(
        day
        for day in (early - timedelta(days=offset) for offset in range(1, 5))
        if not calendar.is_session(day.isoformat()) and day.weekday() < 5
    )
    holiday_now = datetime(holiday.year, holiday.month, holiday.day, 10, 1, tzinfo=_NY).astimezone(
        UTC
    )
    assert (
        capture_intraday_window(ledger, forecaster, market, [pair], now=holiday_now, window="10:00")
        == 0
    )
