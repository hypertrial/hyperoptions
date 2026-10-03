"""The chain API keeps quote odds and physical forecasts separate."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from options_api.chain import LoadedChainPage, assemble_covered_calls
from options_api.main import create_app
from options_api.market_calendar import quote_session, regular_session_open
from options_api.market_sources import DividendStatus, TreasuryCurve
from options_api.market_watch import EntryQuote
from options_api.models import (
    HistoricalBar,
    MarketOddsView,
    OptionQuote,
    PredictiveOddsView,
    TickerListing,
)
from stocksweeper.config import Settings
from stocksweeper.forecast.predictive import PredictiveDistribution

from .synthetic import NOW, RATE, SPOT, TODAY, synthetic_context


def test_chain_serializes_predictive_fallback_and_coherent_payoff(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain, info, history, today, now = synthetic_context()
    quoted = chain.rows[4].model_copy(update={"root": "IREN"})
    chain.rows[4] = quoted
    page = assemble_covered_calls(chain, info, history, today, now, "all")

    async def load(*_args, **_kwargs):
        return LoadedChainPage(page, chain, info, history)

    async def fast_curve(_client: httpx.AsyncClient, _now: datetime, **_kwargs) -> None:
        return None

    async def fast_dividend(_ticker: str, now: datetime) -> DividendStatus:
        return DividendStatus("nonpayer", now)

    monkeypatch.setattr("options_api.main.load_covered_calls", load)
    monkeypatch.setattr("options_api.market_watch.fetch_treasury_curve", fast_curve)
    monkeypatch.setattr("options_api.market_watch.fetch_dividend_status", fast_dividend)
    app = create_app(
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(503))
        ),
        clock=lambda: now,
        prefetch_universe=False,
        research_settings=Settings(data_dir=tmp_path),
        predictive_refresh=False,
    )
    distribution = PredictiveDistribution(
        ticker="IREN", status="available", reason=None, method="empirical_scaled",
        as_of=date(2026, 9, 10), expiry_session=date.fromisoformat(quoted.expiration),
        horizon_sessions=11, spot=50.0, daily_volatility=0.02, model_version="test-v1",
        support=3, data_hash="abc123", terminal_prices=(45.0, 50.0, 55.0),
        weights=(0.2, 0.5, 0.3),
    )
    quote = EntryQuote(
        spot=Decimal("50"), stock_ask=Decimal("50.10"),
        bid=quoted.call_bid or Decimal(0), ask=quoted.call_ask or Decimal(0),
        session_date=today, source="nasdaq", fetched_at=chain.fetched_at,
        rate=Decimal("0.04"), valuation_time=now,
        rate_as_of_session=today,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
        monkeypatch.setattr(app.state.market_odds, "schedule", lambda _tickers: None)
        monkeypatch.setattr(
            app.state.market_odds, "schedule_for_chain", lambda *_args: True
        )
        monkeypatch.setattr(
            app.state.market_odds,
            "lookup",
            lambda *_identity: MarketOddsView(
                status="unavailable",
                reason="Reliable call quotes do not bracket this strike",
                source="nasdaq", session_date=today,
            ),
        )
        monkeypatch.setattr(app.state.market_odds, "entry_quote", lambda *_identity: quote)
        monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda _tickers: None)
        monkeypatch.setattr(
            app.state.predictive_odds, "cache_retrieved_at", lambda *_args: now
        )
        monkeypatch.setattr(
            app.state.predictive_odds,
            "lookup",
            lambda *_args, **_kwargs: (
                PredictiveOddsView(
                    status="available", method="empirical_scaled", itm_pct_tenths=300,
                    otm_pct_tenths=700, atm_pct_tenths=0,
                    as_of_session=distribution.as_of,
                    expiry_session=distribution.expiry_session,
                    model_version="test-v1", support=3, data_hash="abc123",
                    price_basis="completed_close",
                ),
                distribution,
            ),
        )
        response = client.get(
            "/api/covered-calls/IREN?moneyness=itm&forecast_model=empirical_scaled"
        )
        default_response = client.get("/api/covered-calls/IREN?moneyness=itm")
        invalid_response = client.get("/api/covered-calls/IREN?forecast_model=unknown")
        rights_unavailable = client.get(
            "/api/covered-calls/IREN?moneyness=itm&forecast_model=iv_physical"
        )
        page = assemble_covered_calls(chain, info, history, today, now, "all")

        def failed_evidence(_entries):
            raise OSError("evidence disk unavailable")

        monkeypatch.setattr(app.state.predictive_odds.ledger, "record_batch", failed_evidence)
        degraded = client.get(
            "/api/covered-calls/IREN?moneyness=itm&forecast_model=empirical_scaled"
        )
        selected_unavailable = client.get(
            "/api/covered-calls/IREN?moneyness=itm&forecast_model=intraday_shadow"
        )

    assert response.status_code == 200
    body = response.json()
    row = next(
        contract
        for group in body["expirations"]
        for contract in group["contracts"]
        if contract["watch_key"] is not None
    )
    assert row["market_odds"]["status"] == "unavailable"
    assert row["predictive_odds"]["status"] == "available"
    assert row["predictive_odds"]["method"] == "empirical_scaled"
    assert [view["method"] for view in row["physical_models"]] == [
        "lognormal_ewma", "empirical_scaled", "student_t_ewma",
        "gjr_garch_t", "ohlc_har", "skew_t_ewma", "egarch_skew_t",
        "markov_switching", "ngboost_pooled", "earnings_jump",
        "iv_physical", "intraday_shadow",
    ]
    assert [view["method"] for view in row["market_models"]] == [
        "regimelib", "constrained_call_curve", "ssvi",
    ]
    by_method = {view["method"]: view for view in row["physical_models"]}
    assert by_method["iv_physical"]["status"] == "unavailable"
    assert by_method["iv_physical"]["reason"] == (
        "rights_cleared_option_history_unavailable"
    )
    assert by_method["earnings_jump"]["status"] == "unavailable"
    assert by_method["earnings_jump"]["reason"] == (
        "verified_release_time_history_unavailable"
    )
    assert row["market_models"][-1]["status"] == "unavailable"
    assert row["market_models"][-1]["reason"] == (
        "rights_cleared_option_history_unavailable"
    )
    assert row["hypothetical_risk"]["forecast_method"] == "empirical_scaled"
    assert row["predictive_odds"]["model_version"] == "test-v1"
    assert row["hypothetical_risk"]["status"] == "available"
    assert row["hypothetical_risk"]["assumed_spot_cents"] == 5010
    assert row["hypothetical_risk"]["assumed_bid_cents"] == int(quoted.call_bid * 100)
    assert row["hypothetical_risk"]["quote_session"] == today.isoformat()
    assert body["chain_fetched_at"] == now.isoformat().replace("+00:00", "Z")
    default_row = next(
        contract for group in default_response.json()["expirations"]
        for contract in group["contracts"] if contract["watch_key"] is not None
    )
    assert default_row["predictive_odds"]["method"] == "lognormal_ewma"
    assert default_row["predictive_odds"]["status"] == "pending"
    assert default_row["hypothetical_risk"]["status"] == "unavailable"
    assert invalid_response.status_code == 422
    assert degraded.status_code == 200
    degraded_row = next(
        contract
        for group in degraded.json()["expirations"]
        for contract in group["contracts"]
        if contract["watch_key"] is not None
    )
    assert degraded_row["market_odds"]["status"] == "unavailable"
    assert degraded_row["predictive_odds"]["status"] == "unavailable"
    assert degraded_row["predictive_odds"]["reason"] == "Forecast evidence unavailable"
    assert degraded_row["hypothetical_risk"]["status"] == "unavailable"
    unavailable_row = next(
        contract for group in selected_unavailable.json()["expirations"]
        for contract in group["contracts"] if contract["watch_key"] is not None
    )
    assert unavailable_row["predictive_odds"]["status"] == "unavailable"
    assert all(
        view["status"] == "unavailable" for view in unavailable_row["physical_models"]
    )
    rights_row = next(
        contract for group in rights_unavailable.json()["expirations"]
        for contract in group["contracts"] if contract["watch_key"] is not None
    )
    assert rights_row["predictive_odds"]["method"] == "iv_physical"
    assert rights_row["predictive_odds"]["status"] == "unavailable"
    assert rights_row["predictive_odds"]["reason"] == (
        "rights_cleared_option_history_unavailable"
    )
    assert rights_row["predictive_odds"]["itm_pct_tenths"] is None
    assert rights_row["hypothetical_risk"]["status"] == "unavailable"
    assert rights_row["hypothetical_risk"]["forecast_method"] == "iv_physical"
    assert next(
        view for view in rights_row["physical_models"]
        if view["method"] == "empirical_scaled"
    )["status"] == "available"


def _curve(as_of: date = TODAY) -> TreasuryCurve:
    return TreasuryCurve(as_of, ((1 / 12, 0.04), (1.0, 0.04), (2.0, 0.04)))


class _ChainFeed:
    """Serves one chain generation until prepare() advances the fetch time."""

    def __init__(self) -> None:
        self.history_calls = 0
        self.generation = 0
        self.prepare()

    def prepare(self) -> None:
        self.generation += 1
        chain, info, history, _today, now = synthetic_context()
        stamp = now + timedelta(seconds=self.generation)
        rows = [row.model_copy(update={"root": "IREN"}) for row in chain.rows]
        self.chain = chain.model_copy(update={"fetched_at": stamp, "rows": rows})
        self.info = info.model_copy(update={"fetched_at": stamp})
        self.history = history

    async def get_chain(self, _ticker: str):
        return self.chain

    async def get_info(self, _ticker: str, _now: datetime):
        return self.info

    async def get_history(self, _ticker: str, _from_date: str):
        self.history_calls += 1
        return self.history


def _install_quotes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    delay: float = 0,
    dividends: DividendStatus | None = None,
    curve: TreasuryCurve | None = None,
    block_fit: bool = False,
) -> threading.Event:
    release = threading.Event()

    async def treasury(
        _client: httpx.AsyncClient, _now: datetime, **_kwargs
    ) -> TreasuryCurve | None:
        if delay:
            await asyncio.sleep(delay)
        return _curve() if curve is None else curve

    async def dividend(_ticker: str, now: datetime) -> DividendStatus:
        if delay:
            await asyncio.sleep(delay)
        return dividends or DividendStatus("nonpayer", now)

    def calculate(*_args: object, **_kwargs: object) -> dict[object, object]:
        if block_fit:
            release.wait(timeout=20)
        return {}

    monkeypatch.setattr("options_api.market_watch.fetch_treasury_curve", treasury)
    monkeypatch.setattr("options_api.market_watch.fetch_dividend_status", dividend)
    monkeypatch.setattr("options_api.market_watch.calculate_market_odds", calculate)
    return release


def _app(tmp_path, clock):
    return create_app(
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(503))
        ),
        clock=clock,
        prefetch_universe=False,
        research_settings=Settings(data_dir=tmp_path),
        predictive_refresh=False,
    )


def _forecast(monkeypatch: pytest.MonkeyPatch, app, expiry: str) -> None:
    distribution = PredictiveDistribution(
        ticker="IREN", status="available", reason=None, method="empirical_scaled",
        as_of=date(2026, 9, 10), expiry_session=date.fromisoformat(expiry),
        horizon_sessions=11, spot=float(SPOT), daily_volatility=0.02, model_version="test-v1",
        support=3, data_hash="abc123", terminal_prices=(45.0, 50.0, 55.0),
        weights=(0.2, 0.5, 0.3),
    )
    view = PredictiveOddsView(
        status="available", method="empirical_scaled", itm_pct_tenths=300,
        otm_pct_tenths=700, atm_pct_tenths=0, as_of_session=distribution.as_of,
        expiry_session=distribution.expiry_session, model_version="test-v1", support=3,
        data_hash="abc123", price_basis="completed_close",
    )
    monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda _tickers: None)
    monkeypatch.setattr(app.state.predictive_odds, "cache_retrieved_at", lambda *_args: NOW)
    monkeypatch.setattr(
        app.state.predictive_odds, "lookup", lambda *_args, **_kwargs: (view, distribution)
    )


def _row_with_iv(body: dict) -> dict:
    for group in body["expirations"]:
        for contract in group["contracts"]:
            if contract["iv_pct_tenths"] is not None and contract["expiration"] == "2026-09-25":
                return contract
    raise AssertionError("no implied volatility on the displayed chain")


@pytest.mark.parametrize("route", ["covered-calls", "cash-secured-puts"])
def test_concurrent_chain_responses_keep_their_selected_forecast(
    tmp_path, monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    _install_quotes(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    ledger_calls = 0

    def record_batch(_entries) -> None:
        nonlocal ledger_calls
        ledger_calls += 1
        if ledger_calls == 1:
            entered.set()
            assert release.wait(timeout=10), "first ledger write was not released"

    app = _app(tmp_path, lambda: NOW)
    feed = _ChainFeed()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
        monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
        monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
        monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
        monkeypatch.setattr(app.state.market_odds, "schedule", lambda _tickers: None)
        monkeypatch.setattr(app.state.market_odds, "schedule_for_chain", lambda *_args: False)
        monkeypatch.setattr(app.state.physical_shadow, "submit", lambda _entries: None)
        monkeypatch.setattr(app.state.predictive_odds.ledger, "record_batch", record_batch)
        _forecast(monkeypatch, app, "2026-09-25")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                client.get, f"/api/{route}/IREN?forecast_model=empirical_scaled"
            )
            try:
                assert entered.wait(timeout=10), "first request did not reach the ledger"
                second = pool.submit(
                    client.get, f"/api/{route}/IREN?forecast_model=lognormal_ewma"
                ).result(timeout=10)
            finally:
                release.set()
            first_response = first.result(timeout=10)

    for response, method in (
        (first_response, "empirical_scaled"), (second, "lognormal_ewma")
    ):
        assert response.status_code == 200
        rows = [row for group in response.json()["expirations"] for row in group["contracts"]]
        assert rows
        assert all(row["predictive_odds"]["method"] == method for row in rows)
        assert all(row["hypothetical_risk"]["forecast_method"] == method for row in rows)


def test_chain_page_greeks_arrive_while_the_market_fit_is_blocked(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _install_quotes(monkeypatch, block_fit=True)
    app = _app(tmp_path, lambda: NOW)
    feed = _ChainFeed()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
            monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
            monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
            monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
            _forecast(monkeypatch, app, "2026-09-25")
            started = time.perf_counter()
            response = client.get("/api/covered-calls/IREN?forecast_model=empirical_scaled")
            assert time.perf_counter() - started < 4
            assert response.status_code == 200
            row = _row_with_iv(response.json())
            assert row["greeks_source"] == "mid"
            assert row["delta_e4"] is not None
            assert row["greeks_rate_pct_tenths"] == 40
            assert row["greeks_rate_as_of_session"] == TODAY.isoformat()
            assert row["market_odds"]["status"] == "pending"
            assert row["hypothetical_risk"]["status"] == "available"
            assert row["hypothetical_risk"]["assumed_bid_cents"] == row["call_bid_cents"]
            assert row["hypothetical_risk"]["assumed_spot_cents"] == 5010

            first_fetch = response.json()["chain_fetched_at"]
            feed.prepare()
            rolled = client.get("/api/covered-calls/IREN?forecast_model=empirical_scaled")
            assert rolled.status_code == 200
            body = rolled.json()
            assert body["chain_fetched_at"] != first_fetch
            rolled_row = _row_with_iv(body)
            assert rolled_row["greeks_source"] == "mid"
            assert rolled_row["market_odds"]["status"] == "pending"
            assert rolled_row["hypothetical_risk"]["status"] == "available"
        finally:
            release.set()


def test_put_page_greeks_arrive_while_the_market_fit_is_blocked(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _install_quotes(monkeypatch, block_fit=True)
    app = _app(tmp_path, lambda: NOW)
    feed = _ChainFeed()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
            monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
            monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
            monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
            _forecast(monkeypatch, app, "2026-09-25")
            response = client.get("/api/cash-secured-puts/IREN?forecast_model=empirical_scaled")
            assert response.status_code == 200
            row = _row_with_iv(response.json())
            assert row["greeks_source"] == "mid"
            assert row["greeks_rate_pct_tenths"] == 40
            assert row["market_odds"]["status"] == "pending"
            assert row["hypothetical_risk"]["status"] == "available"
            assert row["hypothetical_risk"]["assumed_bid_cents"] == row["put_bid_cents"]
        finally:
            release.set()


def test_slow_treasury_read_does_not_hold_the_chain_page(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _install_quotes(monkeypatch, delay=6, block_fit=True)
    app = _app(tmp_path, lambda: NOW)
    feed = _ChainFeed()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
            monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
            monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
            monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
            monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda _tickers: None)
            started = time.perf_counter()
            response = client.get("/api/covered-calls/IREN")
            assert time.perf_counter() - started < 3.5
            assert response.status_code == 200
            contracts = [
                contract
                for group in response.json()["expirations"]
                for contract in group["contracts"]
            ]
            assert contracts
            assert all(contract["iv_pct_tenths"] is None for contract in contracts)
        finally:
            release.set()


def test_overlapping_chain_pages_share_one_treasury_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _install_quotes(monkeypatch, block_fit=True)
    entered = {"count": 0}

    async def treasury(_client: httpx.AsyncClient, _now: datetime, **_kwargs) -> TreasuryCurve:
        entered["count"] += 1
        await asyncio.sleep(3)
        return _curve()

    async def dividend(_ticker: str, now: datetime) -> DividendStatus:
        await asyncio.sleep(3)
        return DividendStatus("nonpayer", now)

    monkeypatch.setattr("options_api.market_watch.fetch_treasury_curve", treasury)
    monkeypatch.setattr("options_api.market_watch.fetch_dividend_status", dividend)
    app = _app(tmp_path, lambda: NOW)
    feed = _ChainFeed()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
            monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
            monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
            monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
            monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda _tickers: None)
            monkeypatch.setattr(app.state.market_odds, "schedule", lambda _tickers: None)
            monkeypatch.setattr(
                app.state.market_odds, "schedule_for_chain", lambda *_args, **_kwargs: False
            )
            first = client.get("/api/covered-calls/IREN")
            assert first.status_code == 200
            assert all(
                contract["iv_pct_tenths"] is None
                for group in first.json()["expirations"]
                for contract in group["contracts"]
            )
            assert entered["count"] == 1
            second = client.get("/api/covered-calls/IREN")
            assert second.status_code == 200
            assert _row_with_iv(second.json())["greeks_source"] == "mid"
            assert entered["count"] == 1
        finally:
            release.set()


CLOSED = datetime(2026, 9, 11, 21, tzinfo=UTC)


def test_chain_finishing_after_the_close_does_not_price_the_opening_bid_ask(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert regular_session_open(NOW)
    assert not regular_session_open(CLOSED)
    assert quote_session(NOW) == quote_session(CLOSED)
    release = _install_quotes(monkeypatch, block_fit=True)
    moment = {"now": NOW}
    app = _app(tmp_path, lambda: moment["now"])
    feed = _ChainFeed()

    async def get_chain(_ticker: str):
        moment["now"] = CLOSED
        return feed.chain

    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
            monkeypatch.setattr(app.state.service, "get_chain", get_chain)
            monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
            monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
            monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda _tickers: None)
            response = client.get("/api/covered-calls/IREN")
            assert response.status_code == 200
            assert all(
                contract["iv_pct_tenths"] is None
                for group in response.json()["expirations"]
                for contract in group["contracts"]
            )
        finally:
            release.set()


SATURDAY = datetime(2026, 9, 26, 16, tzinfo=UTC)
FRIDAY = date(2026, 9, 25)


def _closed_feed(with_close: bool) -> _ChainFeed:
    feed = _ChainFeed()
    stamp = SATURDAY
    rows = [
        row.model_copy(update={"expiration": "2026-10-16", "root": "IREN"})
        for row in feed.chain.rows
        if row.expiration == "2026-09-25"
    ]
    feed.chain = feed.chain.model_copy(update={"fetched_at": stamp, "rows": rows})
    feed.info = feed.info.model_copy(update={"fetched_at": stamp})
    bars = list(feed.history.bars)
    if with_close:
        bars.append(HistoricalBar(date=FRIDAY, low=Decimal("49"), close=Decimal("50")))
    feed.history = feed.history.model_copy(update={"bars": bars})
    return feed


async def _no_extra_history_fetch(_self, _ticker: str, _now: datetime) -> Decimal | None:
    return None


def test_after_hours_greeks_use_the_completed_close_already_on_the_page(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _install_quotes(monkeypatch, curve=_curve(FRIDAY), block_fit=True)
    monkeypatch.setattr(
        "options_api.market_watch.MarketWatchOdds._completed_session_close",
        _no_extra_history_fetch,
    )
    app = _app(tmp_path, lambda: SATURDAY)
    feed = _closed_feed(with_close=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
            monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
            monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
            monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
            monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda _tickers: None)
            response = client.get("/api/covered-calls/IREN")
            assert response.status_code == 200
            body = response.json()
            row = next(
                contract
                for group in body["expirations"]
                for contract in group["contracts"]
                if contract["iv_pct_tenths"] is not None
            )
            assert row["greeks_source"] == "mid"
            assert row["greeks_rate_as_of_session"] == FRIDAY.isoformat()
            assert row["market_odds"]["status"] == "pending"
            assert feed.history_calls == 1
        finally:
            release.set()


def test_after_hours_without_a_close_falls_back_without_another_history_fetch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _install_quotes(monkeypatch, curve=_curve(FRIDAY), block_fit=True)
    monkeypatch.setattr(
        "options_api.market_watch.MarketWatchOdds._completed_session_close",
        _no_extra_history_fetch,
    )
    app = _app(tmp_path, lambda: SATURDAY)
    feed = _closed_feed(with_close=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
            monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
            monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
            monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
            monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda _tickers: None)
            response = client.get("/api/covered-calls/IREN")
            assert response.status_code == 200
            contracts = [
                contract
                for group in response.json()["expirations"]
                for contract in group["contracts"]
            ]
            assert contracts
            assert all(contract["iv_pct_tenths"] is None for contract in contracts)
            # The page loader reads history once. Pricing must not fetch it again.
            assert feed.history_calls == 1
        finally:
            release.set()


def test_dividend_ineligible_expiry_ignores_a_snapshot_rate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _install_quotes(
        monkeypatch, dividends=DividendStatus("payer", NOW), block_fit=False
    )
    app = _app(tmp_path, lambda: NOW)
    feed = _ChainFeed()
    quoted = next(row for row in feed.chain.rows if row.expiration == "2026-09-25")
    snapshot = EntryQuote(
        spot=SPOT, stock_ask=Decimal("50.10"),
        bid=quoted.call_bid or Decimal("1"), ask=quoted.call_ask or Decimal("1.10"),
        session_date=TODAY, source="nasdaq", fetched_at=feed.chain.fetched_at,
        rate=RATE, valuation_time=NOW, rate_as_of_session=TODAY,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
            monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
            monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
            monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
            monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda _tickers: None)
            monkeypatch.setattr(app.state.market_odds, "entry_quote", lambda *_args: snapshot)
            response = client.get("/api/covered-calls/IREN")
            assert response.status_code == 200
            contracts = [
                contract
                for group in response.json()["expirations"]
                for contract in group["contracts"]
                if contract["expiration"] == "2026-09-25" and contract["watch_key"] is not None
            ]
            assert contracts
            assert all(contract["iv_pct_tenths"] is None for contract in contracts)
            assert all(contract["greeks_source"] is None for contract in contracts)
        finally:
            release.set()


def test_same_day_expiry_ignores_a_snapshot_rate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _install_quotes(monkeypatch, block_fit=False)
    app = _app(tmp_path, lambda: NOW)
    chain, info, history, _today, now = synthetic_context()
    row = OptionQuote(
        ticker="IREN", root="IREN", expiration=TODAY.isoformat(), strike=Decimal("48"),
        call_bid=Decimal("2.20"), call_ask=Decimal("2.40"), call_open_interest=20,
        put_bid=Decimal("0.40"), put_ask=Decimal("0.50"), put_open_interest=20,
    )
    chain = chain.model_copy(update={"rows": [row]})
    feed = _ChainFeed()
    feed.chain = chain
    feed.info = info
    feed.history = history
    snapshot = EntryQuote(
        spot=SPOT, stock_ask=Decimal("50.10"), bid=Decimal("2.20"), ask=Decimal("2.40"),
        session_date=TODAY, source="nasdaq", fetched_at=chain.fetched_at,
        rate=RATE, valuation_time=now, rate_as_of_session=TODAY,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
            monkeypatch.setattr(app.state.service, "get_chain", feed.get_chain)
            monkeypatch.setattr(app.state.service, "get_info", feed.get_info)
            monkeypatch.setattr(app.state.service, "get_history", feed.get_history)
            monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda _tickers: None)
            monkeypatch.setattr(app.state.market_odds, "entry_quote", lambda *_args: snapshot)
            response = client.get("/api/covered-calls/IREN")
            assert response.status_code == 200
            contracts = [
                contract
                for group in response.json()["expirations"]
                for contract in group["contracts"]
            ]
            assert len(contracts) == 1
            assert contracts[0]["iv_pct_tenths"] is None
            assert contracts[0]["greeks_source"] is None
        finally:
            release.set()
