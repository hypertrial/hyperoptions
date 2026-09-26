from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from options_api.cache import TickerCache
from options_api.contract_identity import make_watch_key
from options_api.market_calendar import session_close
from options_api.market_odds import OddsEstimate
from options_api.market_sources import DividendStatus, TreasuryCurve
from options_api.market_watch import MarketWatchOdds
from options_api.service import OptionChainService
from options_api.watchlist import WatchStore
from options_api.models import (
    HistoricalBar,
    HistoricalResponse,
    OptionChainResponse,
    OptionQuote,
    StockInfoResponse,
)


NOW = datetime(2026, 9, 11, 14, tzinfo=UTC)
EXPIRY = "2026-11-20"


class FakeService:
    def __init__(
        self, *, expiration: str = EXPIRY, spot_time: str = "Sep 11, 2026 10:00 AM ET",
        truncated: bool = False,
    ) -> None:
        self.calls = 0
        self.expiration = expiration
        self.spot_time = spot_time
        self.truncated = truncated

    async def get_chain(self, ticker: str) -> OptionChainResponse:
        self.calls += 1
        return OptionChainResponse(
            ticker=ticker, fetched_at=NOW, from_cache=False, last_trade=None,
            source="nasdaq", truncated=self.truncated, rows=[OptionQuote(
                ticker=ticker, root=ticker, expiration=self.expiration,
                strike=Decimal("100"), call_bid=Decimal("3"), call_ask=Decimal("3.1"),
                call_open_interest=50, put_bid=Decimal("2"), put_ask=Decimal("2.1"),
                put_open_interest=50,
            )],
        )

    async def get_info(self, ticker: str, now: datetime) -> StockInfoResponse:
        return StockInfoResponse(
            ticker=ticker, fetched_at=NOW, from_cache=False, bid=Decimal("99.99"),
            ask=Decimal("100.01"), quote_timestamp=self.spot_time,
            is_real_time=True, market_session="Market",
        )


def _curve(as_of: date = date(2026, 9, 11)) -> TreasuryCurve:
    return TreasuryCurve(as_of, ((1 / 12, 0.04), (1.0, 0.04), (2.0, 0.04)))


def _install_inputs(
    monkeypatch: pytest.MonkeyPatch, *, dividends: DividendStatus | None = None,
    curve: TreasuryCurve | None = None, probability: float = 0.4,
    spots: list[Decimal] | None = None,
    valuation_times: list[datetime] | None = None,
) -> list[int]:
    calls: list[int] = []

    async def treasury(client: httpx.AsyncClient, now: datetime) -> TreasuryCurve | None:
        return curve if curve is not None else _curve()

    async def dividend(ticker: str, now: datetime) -> DividendStatus:
        return dividends or DividendStatus("nonpayer", now)

    def calculate(rows, spot, rate_for_expiry, allowed_expirations, as_of):
        calls.append(len(rows))
        if spots is not None:
            spots.append(spot)
        if valuation_times is not None:
            valuation_times.append(as_of)
        return {(row.expiration, row.strike): OddsEstimate(probability) for row in rows}

    monkeypatch.setattr("options_api.market_watch.fetch_treasury_curve", treasury)
    monkeypatch.setattr("options_api.market_watch.fetch_dividend_status", dividend)
    monkeypatch.setattr("options_api.market_watch.calculate_market_odds", calculate)
    return calls


@pytest.mark.asyncio
async def test_one_ticker_refresh_is_shared_by_calls_puts_and_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calculations = _install_inputs(monkeypatch)
    service = FakeService()
    moment = [NOW]
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: moment[0])
        odds.schedule(["TEST", "TEST"])
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        assert service.calls == 1
        assert calculations == [1]
        call = odds.lookup("TEST", "call", EXPIRY, Decimal("100"), "TEST")
        put = odds.lookup("TEST", "put", EXPIRY, Decimal("100"), "TEST")
        assert call.status == put.status == "available"
        assert (call.itm_pct_tenths, call.otm_pct_tenths) == (400, 600)
        assert (put.itm_pct_tenths, put.otm_pct_tenths) == (600, 400)
        assert call.source == "nasdaq"
        assert call.session_date == date(2026, 9, 11)
        assert call.fetched_at == NOW
        assert call.model_version is not None
        assert call.model_version.startswith("regimelib-0.1.0")
        assert odds.lookup("TEST", "call", EXPIRY, Decimal("100"), "OTHER").status == "unavailable"

        moment[0] = datetime(2026, 9, 11, 21, tzinfo=UTC)
        retained = odds.lookup("TEST", "call", EXPIRY, Decimal("100"))
        assert retained.status == "pending"
        moment[0] = datetime(2026, 9, 14, 14, tzinfo=UTC)
        assert odds.lookup("TEST", "call", EXPIRY, Decimal("100")).status == "pending"
        await odds.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service", "dividends", "curve", "reason"),
    [
        (FakeService(spot_time="Sep 11, 2026 09:00 AM ET"), None, None, "stale"),
        (FakeService(), DividendStatus("payer", NOW, date(2026, 10, 16)), None, "Dividend"),
        (FakeService(), DividendStatus("unknown", NOW), None, "Dividend status"),
        (FakeService(), None, _curve(date(2026, 8, 31)), "Treasury"),
        (FakeService(expiration="2026-09-11"), None, None, "Same-day"),
    ],
)
async def test_bad_market_inputs_show_reason_and_never_publish_number(
    monkeypatch: pytest.MonkeyPatch,
    service: FakeService,
    dividends: DividendStatus | None,
    curve: TreasuryCurve | None,
    reason: str,
) -> None:
    calculations = _install_inputs(monkeypatch, dividends=dividends, curve=curve)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: NOW)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        view = odds.lookup("TEST", "call", service.expiration, Decimal("100"))
        assert view.status == "unavailable"
        assert reason in (view.reason or "")
        assert view.itm_pct_tenths is None and view.otm_pct_tenths is None
        assert calculations == []
        await odds.close()


SATURDAY = datetime(2026, 9, 26, 16, tzinfo=UTC)
FRIDAY = date(2026, 9, 25)
THURSDAY = date(2026, 9, 24)


class BlankQuoteService(FakeService):
    def __init__(
        self, bars: list[HistoricalBar], fetched_at: datetime = SATURDAY,
    ) -> None:
        super().__init__()
        self.bars = bars
        self.fetched_at = fetched_at
        self.history_calls = 0
        self.history_from: list[str] = []
        self.released: list[str] = []

    async def get_chain(self, ticker: str) -> OptionChainResponse:
        chain = await super().get_chain(ticker)
        return chain.model_copy(update={"fetched_at": self.fetched_at})

    async def get_info(self, ticker: str, now: datetime) -> StockInfoResponse:
        return StockInfoResponse(
            ticker=ticker, fetched_at=now, from_cache=False, bid=None, ask=None,
            quote_timestamp="Sep 24, 2026", is_real_time=False, market_session="Closed",
        )

    async def get_history(self, ticker: str, from_date: str) -> HistoricalResponse:
        self.history_calls += 1
        self.history_from.append(from_date)
        return HistoricalResponse(
            ticker=ticker, fetched_at=SATURDAY, from_cache=False, bars=self.bars,
        )

    def release_history(self, ticker: str, from_date: str) -> None:
        self.released.append(f"{ticker}:{from_date}")


def _bar(session: date, close: str) -> HistoricalBar:
    return HistoricalBar(date=session, close=Decimal(close))


@pytest.mark.asyncio
async def test_closed_session_uses_official_close_when_bid_and_ask_are_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spots: list[Decimal] = []
    valuation_times: list[datetime] = []
    calculations = _install_inputs(
        monkeypatch, curve=_curve(FRIDAY), spots=spots,
        valuation_times=valuation_times,
    )
    service = BlankQuoteService([_bar(THURSDAY, "46.15"), _bar(FRIDAY, "44.125")])
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: SATURDAY)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        view = odds.lookup("TEST", "call", EXPIRY, Decimal("100"))
        assert view.status == "available"
        assert view.itm_pct_tenths == 400
        assert view.session_date == FRIDAY
        assert spots == [Decimal("44.125")]
        assert valuation_times == [datetime(2026, 9, 25, 20, tzinfo=UTC)]
        assert calculations == [1]
        assert service.history_calls == 1
        assert service.history_from == [(FRIDAY - timedelta(days=10)).isoformat()]
        assert service.released == []
        await odds.close()


def _watch_store(data_dir: Path, side: str = "call") -> WatchStore:
    store = WatchStore(data_dir)
    store.add(
        make_watch_key("TEST", "TEST", side, EXPIRY, Decimal("100")),
        "TEST", "TEST", side, date.fromisoformat(EXPIRY), Decimal("100"),
        NOW - timedelta(minutes=1),
    )
    return store


@pytest.mark.asyncio
async def test_last_good_survives_refresh_failure_and_restart_but_expires_with_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _install_inputs(monkeypatch)
    store = _watch_store(tmp_path)
    service = FakeService()
    moment = [NOW]
    def watched():
        return [(record.watch_key, record.created_at) for record in store.list()]
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: moment[0],
                               data_dir=tmp_path, watched_contracts=watched)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        assert odds.lookup_last_good("TEST", "call", EXPIRY, Decimal("100"), "TEST") is None
        moment[0] = datetime(2026, 9, 11, 21, tzinfo=UTC)
        service.truncated = True
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        assert odds.lookup("TEST", "call", EXPIRY, Decimal("100")).status == "unavailable"
        prior = odds.lookup_last_good("TEST", "call", EXPIRY, Decimal("100"), "TEST")
        assert prior is not None and prior.status == "available"
        assert prior.itm_pct_tenths == 400
        assert prior.fetched_at == NOW and prior.session_date == NOW.date()
        await odds.close()

        restarted = MarketWatchOdds(service, client, lambda: moment[0],
                                    data_dir=tmp_path, watched_contracts=watched)
        assert restarted.lookup_last_good("TEST", "put", EXPIRY, Decimal("100")) is None
        assert restarted.lookup_last_good("TEST", "call", EXPIRY, Decimal("100")) == prior
        moment[0] = datetime(2026, 9, 14, 14, tzinfo=UTC)
        assert restarted.lookup_last_good("TEST", "call", EXPIRY, Decimal("100")) == prior
        record = store.list()[0]
        assert store.delete(record.id)
        store.add(
            record.watch_key, "TEST", "TEST", "call", date.fromisoformat(EXPIRY),
            Decimal("100"), moment[0],
        )
        restarted.schedule([])
        assert restarted.lookup_last_good("TEST", "call", EXPIRY, Decimal("100")) is None
        moment[0] = datetime(2026, 9, 14, 21, tzinfo=UTC)
        assert restarted.lookup_last_good("TEST", "call", EXPIRY, Decimal("100")) is None
        restarted.schedule([])
        await restarted.close()


@pytest.mark.asyncio
async def test_entry_quote_and_rate_require_validated_current_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _install_inputs(monkeypatch, curve=_curve(date(2026, 9, 9)))
    store = _watch_store(tmp_path)
    def watched():
        return [(record.watch_key, record.created_at) for record in store.list()]
    async with httpx.AsyncClient() as client:
        service = FakeService()
        odds = MarketWatchOdds(service, client, lambda: NOW,
                               data_dir=tmp_path, watched_contracts=watched)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        quote = odds.entry_quote("TEST", "call", EXPIRY, Decimal("100"), "TEST")
        assert quote is not None
        assert (quote.spot, quote.stock_ask, quote.bid, quote.ask) == (
            Decimal("100"), Decimal("100.01"), Decimal("3"), Decimal("3.1")
        )
        assert odds.rate_for("TEST", EXPIRY) == (Decimal("0.04"), NOW)
        assert quote.session_date == date(2026, 9, 11)
        assert quote.rate_as_of_session == date(2026, 9, 9)
        assert odds.entry_quote("TEST", "call", EXPIRY, Decimal("100"), "OTHER") is None
        await odds.close()

    # A matching strike with adjusted terms cannot supply an entry quote or saved odds.
    class AdjustedService(FakeService):
        async def get_chain(self, ticker: str) -> OptionChainResponse:
            chain = await super().get_chain(ticker)
            chain.rows[0].root = "OTHER"
            chain.rows[0].identity_reason = "Adjusted contract"
            return chain

    async with httpx.AsyncClient() as client:
        moment = [NOW]
        adjusted = MarketWatchOdds(AdjustedService(), client, lambda: moment[0],
                                   data_dir=tmp_path, watched_contracts=watched)
        adjusted.schedule(["TEST"])
        await asyncio.gather(*adjusted._tasks.values())
        assert adjusted.entry_quote("TEST", "call", EXPIRY, Decimal("100")) is None
        moment[0] = datetime(2026, 9, 11, 21, tzinfo=UTC)
        assert adjusted.lookup_last_good("TEST", "call", EXPIRY, Decimal("100")) is None
        await adjusted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dividends", "curve", "reason"),
    [
        (None, _curve(date(2026, 8, 31)), "Treasury"),
        (DividendStatus("payer", NOW, date(2026, 10, 16)), _curve(), "Dividend"),
    ],
)
async def test_market_rate_or_dividend_gate_does_not_discard_coherent_entry_quotes(
    monkeypatch: pytest.MonkeyPatch,
    dividends: DividendStatus | None,
    curve: TreasuryCurve,
    reason: str,
) -> None:
    calculations = _install_inputs(monkeypatch, dividends=dividends, curve=curve)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(FakeService(), client, lambda: NOW)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        assert reason in (odds.lookup("TEST", "call", EXPIRY, Decimal("100")).reason or "")
        assert calculations == []
        for side in ("call", "put"):
            quote = odds.entry_quote("TEST", side, EXPIRY, Decimal("100"))
            assert quote is not None
            assert quote.rate is None
            assert quote.spot == Decimal("100")
            assert quote.bid == (Decimal("3") if side == "call" else Decimal("2"))
        await odds.close()


@pytest.mark.asyncio
async def test_unwatched_chain_contract_has_coherent_entry_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_inputs(monkeypatch)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(FakeService(), client, lambda: NOW)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        quote = odds.entry_quote("TEST", "put", EXPIRY, Decimal("100"), "TEST")
        assert quote is not None
        assert (quote.bid, quote.ask, quote.spot) == (
            Decimal("2"), Decimal("2.1"), Decimal("100")
        )
        assert quote.fetched_at == NOW and quote.source == "nasdaq"
        await odds.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_stamp", [
    "Sep 25, 2026 10:00 AM ET", "Sep 25, 2026 03:59 PM ET",
])
@pytest.mark.parametrize("has_close", [False, True])
async def test_after_hours_old_stock_ask_is_not_used_as_covered_call_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    quote_stamp: str, has_close: bool,
) -> None:
    spots: list[Decimal] = []
    _install_inputs(monkeypatch, curve=_curve(FRIDAY), spots=spots)
    store = _watch_store(tmp_path)

    def watched():
        return [(record.watch_key, record.created_at) for record in store.list()]

    class AfterHoursService(FakeService):
        history_calls = 0

        async def get_chain(self, ticker: str) -> OptionChainResponse:
            chain = await super().get_chain(ticker)
            return chain.model_copy(update={"fetched_at": SATURDAY})

        async def get_info(self, ticker: str, now: datetime) -> StockInfoResponse:
            info = await super().get_info(ticker, now)
            return info.model_copy(update={"fetched_at": SATURDAY})

        async def get_history(self, ticker: str, from_date: str) -> HistoricalResponse:
            self.history_calls += 1
            bar = _bar(FRIDAY, "44.125") if has_close else _bar(THURSDAY, "46.15")
            return HistoricalResponse(
                ticker=ticker, fetched_at=SATURDAY, from_cache=False, bars=[bar],
            )

        def release_history(self, ticker: str, from_date: str) -> None:
            pass

    async with httpx.AsyncClient() as client:
        service = AfterHoursService(spot_time=quote_stamp)
        odds = MarketWatchOdds(service,
                               client, lambda: SATURDAY, data_dir=tmp_path,
                               watched_contracts=watched)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        quote = odds.entry_quote("TEST", "call", EXPIRY, Decimal("100"))
        assert service.history_calls == 1
        if has_close:
            assert quote is not None
            assert quote.spot == Decimal("44.125")
            assert quote.stock_ask is None
            assert quote.valuation_time == session_close(FRIDAY)
            assert spots == [Decimal("44.125")]
        else:
            assert quote is None
            assert odds.lookup("TEST", "call", EXPIRY, Decimal("100")).reason == (
                "Official completed-session close is unavailable"
            )
            assert spots == []
        await odds.close()


@pytest.mark.asyncio
async def test_early_close_valuation_uses_exchange_close_not_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    early_day = date(2026, 11, 27)
    later = datetime(2026, 11, 28, 16, tzinfo=UTC)
    valuation_times: list[datetime] = []
    _install_inputs(monkeypatch, curve=_curve(early_day), valuation_times=valuation_times)

    class EarlyCloseService(FakeService):
        async def get_chain(self, ticker: str) -> OptionChainResponse:
            chain = await super().get_chain(ticker)
            return chain.model_copy(update={"fetched_at": later})

        async def get_info(self, ticker: str, now: datetime) -> StockInfoResponse:
            info = await super().get_info(ticker, now)
            return info.model_copy(update={"fetched_at": later})

        async def get_history(self, ticker: str, from_date: str) -> HistoricalResponse:
            return HistoricalResponse(
                ticker=ticker, fetched_at=later, from_cache=False,
                bars=[_bar(early_day, "100")],
            )

    service = EarlyCloseService(
        expiration="2027-01-08", spot_time="Nov 27, 2026 12:59 PM ET"
    )
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: later)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        assert valuation_times == [session_close(early_day)]
        assert valuation_times[0] == datetime(2026, 11, 27, 18, tzinfo=UTC)
        await odds.close()


@pytest.mark.asyncio
async def test_chain_refresh_tracks_new_quote_snapshot_even_after_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_inputs(monkeypatch, curve=_curve(FRIDAY))
    service = BlankQuoteService([_bar(FRIDAY, "100")])
    target = SATURDAY + timedelta(minutes=1)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: SATURDAY)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        assert odds.schedule_for_chain("TEST", SATURDAY, "nasdaq")
        service.fetched_at = target
        assert not odds.schedule_for_chain("TEST", target, "nasdaq")
        await asyncio.gather(*odds._tasks.values())
        assert service.calls == 2
        assert odds.schedule_for_chain("TEST", target, "nasdaq")
        assert not odds._tasks
        await odds.close()


@pytest.mark.asyncio
async def test_chain_mismatch_does_not_loop_on_same_target_or_source_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_inputs(monkeypatch)
    moment = [NOW]

    class YahooService(FakeService):
        async def get_chain(self, ticker: str) -> OptionChainResponse:
            chain = await super().get_chain(ticker)
            return chain.model_copy(update={
                "source": "yahoo", "spot": Decimal("100"),
                "last_trade_timestamp": NOW.isoformat(),
            })

    service = YahooService()
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: moment[0])
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        assert not odds.schedule_for_chain("TEST", NOW, "nasdaq")
        await asyncio.gather(*odds._tasks.values())
        assert service.calls == 2
        moment[0] += timedelta(seconds=40)
        assert not odds.schedule_for_chain("TEST", moment[0], "nasdaq")
        assert not odds._tasks
        assert service.calls == 2
        await odds.close()


@pytest.mark.asyncio
async def test_closed_session_without_that_close_does_not_use_an_older_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calculations = _install_inputs(monkeypatch, curve=_curve(FRIDAY))
    service = BlankQuoteService([_bar(THURSDAY, "46.15")])
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: SATURDAY)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        view = odds.lookup("TEST", "call", EXPIRY, Decimal("100"))
        assert view.status == "unavailable"
        assert view.itm_pct_tenths is None and view.otm_pct_tenths is None
        assert view.reason == "Official completed-session close is unavailable"
        assert calculations == []
        assert service.history_calls == 1
        assert service.released == [f"TEST:{(FRIDAY - timedelta(days=10)).isoformat()}"]
        await odds.close()


@pytest.mark.asyncio
async def test_open_session_still_requires_a_coherent_bid_and_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calculations = _install_inputs(monkeypatch)
    service = BlankQuoteService([_bar(date(2026, 9, 11), "100")], fetched_at=NOW)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: NOW)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        view = odds.lookup("TEST", "call", EXPIRY, Decimal("100"))
        assert view.status == "unavailable"
        assert view.reason == "A coherent underlying bid and ask is unavailable"
        assert view.itm_pct_tenths is None
        assert calculations == []
        assert service.history_calls == 0
        await odds.close()


def test_release_history_drops_a_cached_window_before_its_day_long_ttl() -> None:
    cache = TickerCache(ttl_seconds=86_400)
    service = OptionChainService(client=object(), history_cache=cache)  # type: ignore[arg-type]
    key = "TEST:2026-09-15"
    cache._entries[key] = (cache._monotonic(), object())
    service.release_history("TEST", "2026-09-15")
    assert key not in cache._entries


@pytest.mark.asyncio
async def test_truncated_nasdaq_chain_never_publishes_odds_from_partial_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calculations = _install_inputs(monkeypatch)
    service = FakeService(truncated=True)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: NOW)
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        view = odds.lookup("TEST", "call", EXPIRY, Decimal("100"))
        assert view.status == "unavailable"
        assert view.itm_pct_tenths is None
        assert "incomplete" in (view.reason or "").lower()
        assert calculations == []
        await odds.close()


@pytest.mark.asyncio
async def test_slow_yahoo_snapshot_checks_underlying_freshness_at_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calculations = _install_inputs(monkeypatch)
    completed = NOW + timedelta(seconds=75)
    quote_time = completed - timedelta(seconds=5)
    moment = [NOW]

    class SlowYahooService(FakeService):
        async def get_chain(self, ticker: str) -> OptionChainResponse:
            await asyncio.sleep(0)
            moment[0] = completed
            chain = await super().get_chain(ticker)
            return chain.model_copy(update={
                "source": "yahoo", "fetched_at": completed,
                "spot": Decimal("100"), "last_trade_timestamp": quote_time.isoformat(),
            })

    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(SlowYahooService(), client, lambda: moment[0])
        odds.schedule(["TEST"])
        await asyncio.gather(*odds._tasks.values())
        view = odds.lookup("TEST", "call", EXPIRY, Decimal("100"))
        assert view.status == "available"
        assert view.itm_pct_tenths == 400
        assert view.fetched_at == completed
        assert calculations == [1]
        await odds.close()


async def _wait_for_scheduled_refreshes(odds: MarketWatchOdds) -> None:
    async def settled() -> None:
        while odds._tasks or odds._pending:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(settled(), timeout=10)


@pytest.mark.asyncio
async def test_hundred_watched_tickers_eventually_refresh_in_fifo_order() -> None:
    release = asyncio.Event()
    seen: list[str] = []
    symbols = [f"T{index:03d}" for index in range(100)]

    class SlowService(FakeService):
        async def get_chain(self, ticker: str) -> OptionChainResponse:
            seen.append(ticker)
            await release.wait()
            return await super().get_chain(ticker)

    service = SlowService(truncated=True)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: NOW)
        for symbol in symbols:
            odds.schedule([symbol])
        for _ in range(20):
            odds.schedule([symbols[0]])
        await asyncio.sleep(0)
        assert len(odds._tasks) == 8
        assert len(odds._pending) == 92
        release.set()
        await _wait_for_scheduled_refreshes(odds)
        assert seen == symbols
        assert service.calls == 100
        assert odds.lookup(symbols[-1], "call", EXPIRY, Decimal("100")).status == "unavailable"
        await odds.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["truncated", "error"])
async def test_error_and_truncated_results_share_bounded_cache_eviction(failure: str) -> None:
    class FailingService(FakeService):
        async def get_chain(self, ticker: str) -> OptionChainResponse:
            if failure == "error":
                raise RuntimeError("provider failed")
            return await super().get_chain(ticker)

    service = FailingService(truncated=True)
    async with httpx.AsyncClient() as client:
        odds = MarketWatchOdds(service, client, lambda: NOW)
        for start in range(0, 330, 100):
            odds.schedule(f"T{index:03d}" for index in range(start, min(start + 100, 330)))
            await _wait_for_scheduled_refreshes(odds)
        assert len(odds._cache) == 320
        assert odds.lookup("T000", "call", EXPIRY, Decimal("100")).status == "pending"
        assert odds.lookup("T009", "call", EXPIRY, Decimal("100")).status == "pending"
        first_retained = odds.lookup("T010", "call", EXPIRY, Decimal("100"))
        latest = odds.lookup("T329", "call", EXPIRY, Decimal("100"))
        assert first_retained.status == latest.status == "unavailable"
        expected_reason = "incomplete" if failure == "truncated" else "unavailable"
        assert expected_reason in (latest.reason or "").lower()
        await odds.close()
