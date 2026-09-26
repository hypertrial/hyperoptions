from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from options_api.market_odds import OddsEstimate
from options_api.market_sources import DividendStatus, TreasuryCurve
from options_api.market_watch import MarketWatchOdds
from options_api.models import OptionChainResponse, OptionQuote, StockInfoResponse


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
) -> list[int]:
    calls: list[int] = []

    async def treasury(client: httpx.AsyncClient, now: datetime) -> TreasuryCurve | None:
        return curve if curve is not None else _curve()

    async def dividend(ticker: str, now: datetime) -> DividendStatus:
        return dividends or DividendStatus("nonpayer", now)

    def calculate(rows, spot, rate_for_expiry, allowed_expirations, as_of):
        calls.append(len(rows))
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
        assert retained.status == "available"
        assert retained.session_date == date(2026, 9, 11)
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
