from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from options_api.chain import assemble_cash_secured_puts, assemble_covered_calls
from options_api.contract_identity import make_watch_key, parse_watch_key, row_identity
from options_api.market_calendar import latest_completed_session, session_on_or_before
from options_api.models import OptionChainResponse, OptionQuote, StockInfoResponse
from options_api.models import TickerListing
from options_api.main import create_app
from options_api.outcomes import (
    CloseHistory,
    OutcomeResult,
    YahooCloseProvider,
    classify,
    resolve_outcome,
)
from options_api.parser import parse_option_chain
from options_api.watchlist import WatchStore, WatchlistService
from stocksweeper.config import Settings
from stocksweeper.forecast.models import ForecastSnapshot
from stocksweeper.pipeline.jobs import JobBusy, JobManager

from .conftest import load_fixture

D = Decimal


@dataclass
class FakeCloseProvider:
    history: CloseHistory

    def fetch(self, ticker: str, start: date, end: date) -> CloseHistory:
        assert ticker == "IREN"
        assert start <= date(2026, 7, 2) < end
        return self.history


def test_nasdaq_identity_is_exact_and_put_can_share_call_shaped_url() -> None:
    url = "/market-activity/stocks/iren/option-chain/call-put-options/iren--261016c00040500"
    assert row_identity(url, "IREN", "2026-10-16", D("40.500")) == ("IREN", None)
    assert row_identity(url, "IREN", "2026-10-16", D("40.501"))[1] is not None
    assert row_identity(url, "IREN", "2026-10-23", D("40.500"))[1] is not None
    assert (
        row_identity(url.replace("iren--", "iren1--"), "IREN", "2026-10-16", D("40.500"))[1]
        is not None
    )
    assert row_identity(None, "IREN", "2026-10-16", D("40.500"))[1] is not None

    rows, _, _, _ = parse_option_chain("IREN", load_fixture("nasdaq_iren_sample.json"))
    assert rows[0].root == "IREN"
    assert rows[0].identity_reason is None
    assert rows[0].put_bid is not None
    key = make_watch_key("IREN", "IREN", "put", "2026-09-18", rows[0].strike)
    assert parse_watch_key(key) == ("IREN", "IREN", "put", "2026-09-18", D("50.000"))
    assert parse_watch_key(key.replace("50.000", "50.001")) != parse_watch_key(key)
    assert parse_watch_key("w1:IREN:IREN:put:2026-99-99:50.000") is None


def test_atm_is_in_neither_itm_nor_otm_and_expiry_day_is_visible_before_close() -> None:
    now = datetime(2026, 9, 11, 14, tzinfo=UTC)
    quote = OptionQuote(
        ticker="IREN",
        expiration="2026-09-11",
        strike=D("50.000"),
        root="IREN",
        call_bid=D("1"),
        call_ask=D("1.1"),
        call_open_interest=10,
        put_bid=D("1"),
        put_ask=D("1.1"),
        put_open_interest=10,
    )
    chain = OptionChainResponse(
        ticker="IREN",
        fetched_at=now,
        from_cache=False,
        last_trade=None,
        spot=D("50.000"),
        rows=[quote],
    )
    info = StockInfoResponse(
        ticker="IREN",
        fetched_at=now,
        from_cache=False,
        bid=D("50.000"),
        ask=D("50.01"),
        quote_timestamp=None,
        is_real_time=False,
        market_session=None,
    )
    all_calls = assemble_covered_calls(chain, info, None, now.date(), now, "all")
    contract = all_calls.expirations[0].contracts[0]
    assert contract.at_the_money is True
    assert contract.in_the_money is False
    assert contract.watch_key == "w1:IREN:IREN:call:2026-09-11:50.000"
    assert assemble_covered_calls(chain, info, None, now.date(), now, "itm").expirations == []
    assert assemble_covered_calls(chain, info, None, now.date(), now, "otm").expirations == []
    assert assemble_cash_secured_puts(chain, info, None, now.date(), now, "otm").expirations == []
    after_close = datetime(2026, 9, 11, 21, tzinfo=UTC)
    assert (
        assemble_covered_calls(chain, info, None, now.date(), after_close, "all").expirations == []
    )


@pytest.mark.parametrize(
    ("side", "close", "expected"),
    [
        ("call", "50", "atm"),
        ("call", "50.01", "itm"),
        ("call", "49.99", "otm"),
        ("put", "50", "atm"),
        ("put", "49.99", "itm"),
        ("put", "50.01", "otm"),
    ],
)
def test_outcome_strict_strike_boundary(side: str, close: str, expected: str) -> None:
    assert classify(side, D(close), D("50")) == expected


def test_holiday_uses_expected_prior_session_and_missing_close_remains_pending() -> None:
    assert session_on_or_before(date(2026, 7, 3)) == date(2026, 7, 2)
    assert latest_completed_session(datetime(2026, 7, 2, 19, tzinfo=UTC)) == date(2026, 7, 1)
    base = dict(
        ticker="IREN",
        root="IREN",
        side="call",
        strike=D("50"),
        expiration=date(2026, 7, 3),
        watched_at=datetime(2026, 6, 30, tzinfo=UTC),
    )
    provider = FakeCloseProvider(
        CloseHistory({date(2026, 7, 1): D("55")}, frozenset(), frozenset(), True)
    )
    before = resolve_outcome(**base, as_of=datetime(2026, 7, 2, 19, tzinfo=UTC), provider=provider)
    assert before.status == "pending"
    after = resolve_outcome(**base, as_of=datetime(2026, 7, 2, 22, tzinfo=UTC), provider=provider)
    assert after.status == "pending"
    assert after.session_date == date(2026, 7, 2)
    assert after.close_price is None


def test_split_guard_and_provisional_outcome_with_evidence() -> None:
    base = dict(
        ticker="IREN",
        root="IREN",
        side="put",
        strike=D("50"),
        expiration=date(2026, 7, 3),
        watched_at=datetime(2026, 6, 30, tzinfo=UTC),
        as_of=datetime(2026, 7, 7, 22, tzinfo=UTC),
    )
    history = CloseHistory({date(2026, 7, 2): D("49.99")}, frozenset(), frozenset(), True)
    result = resolve_outcome(**base, provider=FakeCloseProvider(history))
    assert result.status == "provisional"
    assert result.classification == "itm"
    assert result.close_price == D("49.99")
    assert result.session_date == date(2026, 7, 2)
    assert result.source is not None and "Yahoo" in result.source
    split = CloseHistory(history.closes, frozenset({date(2026, 7, 6)}), frozenset(), True)
    guarded = resolve_outcome(**base, provider=FakeCloseProvider(split))
    assert guarded.status == "unsupported"
    assert guarded.classification is None
    assert "split" in (guarded.reason or "").lower()
    assert guarded.preserve_prior is True
    pre_expiry_split = CloseHistory(
        history.closes, frozenset({date(2026, 7, 1)}), frozenset(), True
    )
    invalid_terms = resolve_outcome(**base, provider=FakeCloseProvider(pre_expiry_split))
    assert invalid_terms.status == "unsupported"
    assert invalid_terms.preserve_prior is False
    unverified = CloseHistory(history.closes, frozenset(), frozenset(), False)
    retry = resolve_outcome(**base, provider=FakeCloseProvider(unverified))
    assert retry.status == "pending"
    assert retry.classification is None
    assert "history" in (retry.reason or "").lower()


def test_outcome_retrieval_time_is_captured_after_provider_response() -> None:
    clock = [datetime(2026, 7, 7, 22, tzinfo=UTC)]

    class SlowProvider:
        def fetch(self, ticker: str, start: date, end: date) -> CloseHistory:
            clock[0] = datetime(2026, 7, 7, 22, 5, tzinfo=UTC)
            return CloseHistory(
                {date(2026, 7, 2): D("50")}, frozenset(), frozenset(), True
            )

    result = resolve_outcome(
        ticker="IREN",
        root="IREN",
        side="call",
        strike=D("50"),
        expiration=date(2026, 7, 3),
        watched_at=datetime(2026, 6, 30, tzinfo=UTC),
        as_of=clock[0],
        provider=SlowProvider(),
        clock=lambda: clock[0],
    )
    assert result.status == "provisional"
    assert result.classification == "atm"
    assert result.retrieved_at == datetime(2026, 7, 7, 22, 5, tzinfo=UTC)


def test_yahoo_provider_keeps_close_precision_and_requests_unadjusted_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeTicker:
        def history(self, **kwargs):
            captured.update(kwargs)
            return pd.DataFrame(
                {"Close": [49.99999], "Stock Splits": [0.0]},
                index=pd.to_datetime(["2026-07-02"]).tz_localize("America/New_York"),
            )

    monkeypatch.setattr("yfinance.Ticker", lambda ticker: FakeTicker())
    history = YahooCloseProvider().fetch("IREN", date(2026, 7, 2), date(2026, 7, 3))
    assert captured["auto_adjust"] is False
    assert captured["actions"] is True
    assert captured["timeout"] == 20
    assert history.closes[date(2026, 7, 2)] == D("49.99999")
    assert classify("call", history.closes[date(2026, 7, 2)], D("50")) == "otm"


def test_yahoo_nonfinite_split_action_withholds_expiry_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTicker:
        def history(self, **kwargs):
            return pd.DataFrame(
                {"Close": [51.0, 52.0], "Stock Splits": [float("nan"), 0.0]},
                index=pd.to_datetime(["2026-07-01", "2026-07-02"]).tz_localize(
                    "America/New_York"
                ),
            )

    monkeypatch.setattr("yfinance.Ticker", lambda ticker: FakeTicker())
    provider = YahooCloseProvider()
    history = provider.fetch("IREN", date(2026, 7, 1), date(2026, 7, 3))
    assert history.actions_verified is False
    result = resolve_outcome(
        ticker="IREN", root="IREN", side="call", strike=D("50"),
        expiration=date(2026, 7, 3), watched_at=datetime(2026, 7, 1, tzinfo=UTC),
        as_of=datetime(2026, 7, 7, 22, tzinfo=UTC), provider=provider,
    )
    assert result.status == "pending"
    assert "Corporate-action history" in (result.reason or "")


def test_watch_store_dedupes_and_keeps_revisions_across_restart(tmp_path: Path) -> None:
    key = "w1:IREN:IREN:call:2026-07-03:50.000"
    first = WatchStore(tmp_path)
    watched_at = datetime(2026, 6, 30, tzinfo=UTC)
    record, created = first.add(key, "IREN", "IREN", "call", date(2026, 7, 3), D("50"), watched_at)
    assert created
    same, created = first.add(key, "IREN", "IREN", "call", date(2026, 7, 3), D("50"), watched_at)
    assert not created and same.id == record.id
    first.record_outcome(
        record,
        OutcomeResult(
            "provisional",
            "itm",
            None,
            "Yahoo",
            date(2026, 7, 2),
            datetime(2026, 7, 3, tzinfo=UTC),
            D("51"),
        ),
        date(2026, 7, 2),
    )
    restarted = WatchStore(tmp_path)
    assert restarted.list()[0].id == record.id
    restarted.record_outcome(
        record,
        OutcomeResult(
            "provisional",
            "otm",
            None,
            "Yahoo",
            date(2026, 7, 2),
            datetime(2026, 7, 4, tzinfo=UTC),
            D("49"),
        ),
        date(2026, 7, 2),
    )
    outcome = restarted.latest_outcomes()[record.id]
    assert outcome.classification == "otm"
    assert outcome.revised is True
    restarted.record_outcome(
        record,
        OutcomeResult("pending", None, "Yahoo close temporarily unavailable", "Yahoo",
                      date(2026, 7, 2), datetime(2026, 7, 5, tzinfo=UTC), None),
        date(2026, 7, 2),
    )
    assert restarted.latest_outcomes()[record.id].classification == "otm"
    assert restarted.latest_outcomes()[record.id].reason == "Yahoo close temporarily unavailable"
    restarted.record_outcome(
        record,
        OutcomeResult("unsupported", None, "A post-expiry split prevents recheck", "Yahoo",
                      date(2026, 7, 2), datetime(2026, 7, 6, tzinfo=UTC), None,
                      preserve_prior=True),
        date(2026, 7, 2),
    )
    kept = restarted.latest_outcomes()[record.id]
    assert kept.status == "provisional"
    assert kept.classification == "otm"
    assert kept.reason == "A post-expiry split prevents recheck"
    assert kept.retrieved_at == datetime(2026, 7, 4, tzinfo=UTC)
    restarted.record_outcome(
        record,
        OutcomeResult("provisional", "otm", None, "Yahoo", date(2026, 7, 2),
                      datetime(2026, 7, 7, tzinfo=UTC), D("49")),
        date(2026, 7, 2),
    )
    assert restarted.latest_outcomes()[record.id].reason is None
    assert restarted.list()[0].last_attempted_session == date(2026, 7, 2)
    assert restarted.delete(record.id)
    assert restarted.list() == []
    assert restarted.latest_outcomes() == {}


def test_daily_refresh_waits_for_published_session_after_market_close(tmp_path: Path) -> None:
    watchlist = WatchlistService(tmp_path)
    key = "w1:IREN:IREN:call:2026-09-18:50.000"
    record, _ = watchlist.store.add(
        key, "IREN", "IREN", "call", date(2026, 9, 18), D("50"),
        datetime(2026, 9, 10, tzinfo=UTC),
    )
    watchlist.store.mark_attempted(record.id, date(2026, 9, 10))
    jobs = JobManager(tmp_path)
    five_after = datetime(2026, 9, 11, 20, 5, tzinfo=UTC)
    assert watchlist.queue_refresh(jobs, as_of=five_after, candidates=[]) is None
    thirty_one_after = datetime(2026, 9, 11, 20, 31, tzinfo=UTC)
    job = watchlist.queue_refresh(jobs, as_of=thirty_one_after, candidates=[])
    assert job is not None
    assert jobs.wait(2.0)
    assert watchlist.store.list()[0].last_attempted_session == date(2026, 9, 11)


def test_watchlist_get_shows_running_job_ahead_of_queued_job(tmp_path: Path) -> None:
    app = create_app(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(500)
        )),
        prefetch_universe=False,
        research_settings=Settings(data_dir=tmp_path),
    )
    started = Event()
    release = Event()

    def running(progress):
        progress(0.4, "Selecting peers")
        started.set()
        assert release.wait(3)
        return None

    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = app.state.jobs.submit("watch_refresh", running)
        assert started.wait(2)
        second = app.state.jobs.submit("watch_refresh", lambda progress: None)
        body = client.get("/api/watchlist").json()
        assert body["items"] == []
        assert body["active_job"]["id"] == first.id
        assert body["active_job"]["state"] == "running"
        assert body["active_job"]["progress"] == 0.4
        assert second.id != first.id
        release.set()
        assert app.state.jobs.wait(2)
        assert client.get("/api/watchlist").json()["active_job"] is None


def test_queued_refresh_uses_execution_time_after_expiry_close(tmp_path: Path) -> None:
    class ExpiryProvider:
        def fetch(self, ticker: str, start: date, end: date) -> CloseHistory:
            assert ticker == "IREN"
            assert end > date(2026, 9, 18)
            return CloseHistory(
                {date(2026, 9, 18): D("51")}, frozenset(), frozenset(), True
            )

    watchlist = WatchlistService(tmp_path, ExpiryProvider())
    watchlist.store.add(
        "w1:IREN:IREN:call:2026-09-18:50.000", "IREN", "IREN", "call",
        date(2026, 9, 18), D("50"), datetime(2026, 9, 17, tzinfo=UTC),
    )
    jobs = JobManager(tmp_path)
    released = Event()
    jobs.submit("research", lambda progress: released.wait(2.0) and None)
    current = [datetime(2026, 9, 18, 19, tzinfo=UTC)]
    queued = watchlist.queue_refresh(
        jobs, as_of=current[0], candidates=[], clock=lambda: current[0]
    )
    assert queued is not None
    current[0] = datetime(2026, 9, 18, 22, tzinfo=UTC)
    released.set()
    assert jobs.wait(3.0)
    outcome = next(iter(watchlist.store.latest_outcomes().values()))
    assert outcome.status == "provisional"
    assert outcome.classification == "itm"
    assert outcome.retrieved_at == current[0]


def test_watch_added_while_refresh_is_queued_gets_its_own_followup(tmp_path: Path) -> None:
    watchlist = WatchlistService(tmp_path)
    jobs = JobManager(tmp_path)
    released = Event()
    jobs.submit("research", lambda progress: released.wait(2.0) and None)
    now = datetime(2026, 9, 11, 20, 31, tzinfo=UTC)
    first, _ = watchlist.store.add(
        "w1:IREN:IREN:call:2026-09-18:50.000", "IREN", "IREN", "call",
        date(2026, 9, 18), D("50"), now,
    )
    first_job = watchlist.queue_refresh(jobs, as_of=now, candidates=[])
    second, _ = watchlist.store.add(
        "w1:AAPL:AAPL:put:2026-09-18:200.000", "AAPL", "AAPL", "put",
        date(2026, 9, 18), D("200"), now,
    )
    second_job = watchlist.queue_refresh(jobs, as_of=now, candidates=[])
    assert first_job is not None and second_job is not None
    assert first_job.id != second_job.id
    released.set()
    assert jobs.wait(3.0)
    attempts = {item.id: item.last_attempted_session for item in watchlist.store.list()}
    assert attempts[first.id] == attempts[second.id] == date(2026, 9, 11)


def test_each_watch_uses_current_time_during_a_slow_batch(tmp_path: Path) -> None:
    class ExpiryProvider:
        def fetch(self, ticker: str, start: date, end: date) -> CloseHistory:
            assert ticker == "IREN"
            return CloseHistory(
                {date(2026, 9, 18): D("51")}, frozenset(), frozenset(), True
            )

    watchlist = WatchlistService(tmp_path, ExpiryProvider())
    jobs = JobManager(tmp_path)
    now = [datetime(2026, 9, 18, 19, tzinfo=UTC)]
    for strike in (D("49"), D("50")):
        watchlist.store.add(
            make_watch_key("IREN", "IREN", "call", "2026-09-18", strike),
            "IREN", "IREN", "call", date(2026, 9, 18), strike,
            datetime(2026, 9, 17, tzinfo=UTC),
        )

    class SlowForecast:
        def refresh_contract(self, *args, **kwargs) -> None:
            now[0] = datetime(2026, 9, 18, 22, tzinfo=UTC)

    watchlist.forecast = SlowForecast()
    queued = watchlist.queue_refresh(
        jobs, as_of=now[0], candidates=[], clock=lambda: now[0]
    )
    assert queued is not None
    assert jobs.wait(3.0)
    outcomes = watchlist.store.latest_outcomes()
    assert len(outcomes) == 1
    assert next(iter(outcomes.values())).status == "provisional"


def test_missing_forecast_data_retries_on_startup_and_after_backoff(tmp_path: Path) -> None:
    watchlist = WatchlistService(tmp_path)
    now = datetime(2026, 9, 11, 20, 31, tzinfo=UTC)
    record, _ = watchlist.store.add(
        "w1:IREN:IREN:call:2026-09-18:50.000", "IREN", "IREN", "call",
        date(2026, 9, 18), D("50"), now,
    )
    watchlist.store.mark_attempted(record.id, date(2026, 9, 11), attempted_at=now)

    class MissingForecast:
        attempts = 0

        def __init__(self) -> None:
            self.created_at = now

        def latest_snapshot(self, *args) -> ForecastSnapshot:
            return ForecastSnapshot(
                ticker="IREN", side="call", strike=D("50"),
                expiry=date(2026, 9, 18), as_of=date(2026, 9, 11),
                status="unavailable", reason="market_data_missing",
                created_at=self.created_at,
            )

        def refresh_contract(self, *args, **kwargs) -> None:
            self.attempts += 1

    forecast = MissingForecast()
    watchlist.forecast = forecast
    jobs = JobManager(tmp_path)
    assert watchlist.queue_refresh(jobs, as_of=now, candidates=[]) is None
    later = datetime(2026, 9, 11, 21, 2, tzinfo=UTC)
    assert watchlist.queue_refresh(jobs, as_of=later, candidates=[], clock=lambda: later)
    assert jobs.wait(3.0)
    assert forecast.attempts == 1
    restarted = WatchlistService(tmp_path)
    restarted.forecast = forecast
    shortly_after = datetime(2026, 9, 11, 21, 7, tzinfo=UTC)
    assert restarted.queue_refresh(jobs, as_of=shortly_after, candidates=[]) is None
    assert restarted.store.list()[0].last_attempted_at == later
    assert restarted.queue_refresh(
        jobs, as_of=shortly_after, candidates=[], retry_pending=True,
        clock=lambda: shortly_after,
    )
    assert jobs.wait(3.0)
    assert forecast.attempts == 2
    assert restarted.store.list()[0].last_attempted_at == shortly_after


def test_expiry_keeps_dated_available_forecast_beside_latest_unavailable(
    tmp_path: Path,
) -> None:
    watchlist = WatchlistService(tmp_path)
    record, _ = watchlist.store.add(
        "w1:IREN:IREN:call:2026-09-18:50.000", "IREN", "IREN", "call",
        date(2026, 9, 18), D("50"), datetime(2026, 9, 10, tzinfo=UTC),
    )
    watchlist.store.record_outcome(
        record,
        OutcomeResult("provisional", "itm", None, "Yahoo", date(2026, 9, 18),
                      datetime(2026, 9, 19, tzinfo=UTC), D("51")),
        date(2026, 9, 18),
    )

    class FakeForecast:
        def latest_snapshot(self, *args):
            return ForecastSnapshot(
                ticker="IREN", side="call", strike=D("50"), expiry=date(2026, 9, 18),
                as_of=date(2026, 9, 17), status="unavailable", reason="validation_failed",
            )

        def last_available_snapshot(self, *args):
            return ForecastSnapshot(
                ticker="IREN", side="call", strike=D("50"), expiry=date(2026, 9, 18),
                as_of=date(2026, 9, 16), status="available", reason=None,
                itm_probability=0.62,
            )

    watchlist.forecast = FakeForecast()
    item = watchlist.items()[0]
    assert item.outcome.classification == "itm"
    assert item.forecast.status == "unavailable"
    assert item.forecast.reason == "validation_failed"
    assert item.last_available_forecast is not None
    assert item.last_available_forecast.itm_probability == 0.62
    assert item.last_available_forecast.as_of == date(2026, 9, 16)
    assert item.last_available_forecast.historical is True


def test_readded_watch_does_not_inherit_old_forecast_snapshot(tmp_path: Path) -> None:
    watchlist = WatchlistService(tmp_path)
    key = "w1:IREN:IREN:call:2026-09-18:50.000"
    old, _ = watchlist.store.add(
        key, "IREN", "IREN", "call", date(2026, 9, 18), D("50"),
        datetime(2026, 9, 10, tzinfo=UTC),
    )
    assert watchlist.store.delete(old.id)
    new, _ = watchlist.store.add(
        key, "IREN", "IREN", "call", date(2026, 9, 18), D("50"),
        datetime(2026, 9, 11, 20, 31, tzinfo=UTC),
    )

    class PreviousForecast:
        def latest_snapshot(self, *args) -> ForecastSnapshot:
            return ForecastSnapshot(
                ticker="IREN", side="call", strike=D("50"),
                expiry=date(2026, 9, 18), as_of=date(2026, 9, 10),
                status="available", reason=None, itm_probability=0.8,
                created_at=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
            )

    watchlist.forecast = PreviousForecast()
    item = watchlist.item(new)
    assert item.forecast.status == "unavailable"
    assert item.forecast.itm_probability is None


def test_watch_api_revalidates_current_chain_and_blocks_cross_site_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = load_fixture("nasdaq_iren_sample.json")
    fetches = {"chain": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url):
            fetches["chain"] += 1
            return httpx.Response(200, json=payload)
        raise AssertionError(str(request.url))

    app = create_app(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        clock=lambda: datetime(2026, 9, 11, 14, tzinfo=UTC),
        prefetch_universe=False,
        research_settings=Settings(data_dir=tmp_path),
    )
    origin = {"Origin": "http://localhost:5173"}
    key = "w1:IREN:IREN:call:2026-09-18:48.000"

    class FakeForecast:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, Decimal, date, datetime, list]] = []

        def latest_snapshot(self, *args):
            return None

        def refresh_contract(
            self, ticker: str, side: str, strike: Decimal, expiry: date, *,
            as_of: datetime, candidates: list, progress, watched_at: datetime,
            force_rebuild: bool,
        ) -> None:
            progress(0.5, "selecting peers")
            self.calls.append((ticker, side, strike, expiry, as_of, candidates))
            assert watched_at == item_watch_time
            assert isinstance(force_rebuild, bool)

    fake_forecast = FakeForecast()
    item_watch_time = datetime(2026, 9, 11, 14, tzinfo=UTC)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.universe.seed([TickerListing(symbol="IREN", name="IREN", sector="Finance")])
        app.state.watchlist.forecast = fake_forecast
        assert client.get(
            "/api/health", headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code == 403
        assert client.get(
            "/api/health", headers={"Sec-Fetch-Site": "same-origin"}
        ).status_code == 200
        assert client.post("/api/watchlist", json={"watch_key": key}).status_code == 403
        assert (
            client.post(
                "/api/watchlist",
                json={"watch_key": key},
                headers={"Origin": "http://evil.example"},
            ).status_code
            == 403
        )
        assert fetches["chain"] == 0
        altered = key.replace("48.000", "48.001")
        assert (
            client.post("/api/watchlist", json={"watch_key": altered}, headers=origin).status_code
            == 409
        )
        adjusted = key.replace(":IREN:call", ":IREN1:call")
        assert (
            client.post("/api/watchlist", json={"watch_key": adjusted}, headers=origin).status_code
            == 409
        )
        first = client.post("/api/watchlist", json={"watch_key": key}, headers=origin)
        assert first.status_code == 200
        assert first.json()["created"] is True
        item = first.json()["item"]
        assert item["root"] == "IREN"
        assert item["strike_exact"] == "48.000"
        assert item["terms_note"] == "Assuming standard 100-share terms."
        assert item["forecast"]["status"] == "unavailable"
        job = first.json()["job"]
        assert job is not None
        assert app.state.jobs.wait(2.0)
        assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "succeeded"
        assert len(fake_forecast.calls) == 1
        assert fake_forecast.calls[0][:4] == (
            "IREN", "call", D("48.000"), date(2026, 9, 18)
        )
        assert fake_forecast.calls[0][4].tzinfo is not None
        assert fake_forecast.calls[0][5][0].ticker == "IREN"
        second = client.post("/api/watchlist", json={"watch_key": key}, headers=origin)
        assert second.status_code == 200
        assert second.json()["created"] is False
        assert second.json()["item"]["id"] == item["id"]
        assert len(client.get("/api/watchlist").json()["items"]) == 1
        refreshed = client.post("/api/watchlist/refresh", json={}, headers=origin)
        assert refreshed.status_code == 200
        assert refreshed.json()["job"] is not None
        assert app.state.jobs.wait(2.0)
        with monkeypatch.context() as patch:
            patch.setattr(
                app.state.jobs, "submit",
                lambda *args, **kwargs: (_ for _ in ()).throw(JobBusy("full")),
            )
            busy = client.post("/api/watchlist/refresh", json={}, headers=origin)
            assert busy.status_code == 429
            assert busy.json()["detail"] == "Background job queue is busy"
        assert client.delete(f"/api/watchlist/{item['id']}").status_code == 403
        assert client.delete(f"/api/watchlist/{item['id']}", headers=origin).status_code == 204
        assert client.get("/api/watchlist").json()["items"] == []

        class MissingUniverse:
            available = False

            async def ensure(self):
                return False

        old_universe = app.state.universe
        app.state.universe = MissingUniverse()
        assert client.post(
            "/api/watchlist/refresh", json={}, headers=origin
        ).status_code == 503
        app.state.universe = old_universe
