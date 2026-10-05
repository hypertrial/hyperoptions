from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from options_api.cache import TickerCache
from options_api.chain import load_cash_secured_puts, load_covered_calls
from options_api.market_calendar import latest_completed_session
from options_api.models import OptionChainResponse, StockInfoResponse
from options_api.pricing_context import positive_close
from options_api.service import OptionChainService


@pytest.mark.parametrize("loader", [load_covered_calls, load_cash_secured_puts])
@pytest.mark.parametrize(
    "close_at",
    [
        datetime(2026, 9, 25, 20, tzinfo=UTC),
        datetime(2026, 11, 27, 18, tzinfo=UTC),  # Thanksgiving early close, after DST.
    ],
)
async def test_page_retries_missing_completed_close_on_next_request(loader, close_at):
    phase = {"now": close_at - timedelta(minutes=1), "published": False, "calls": 0}
    completed = latest_completed_session(close_at + timedelta(minutes=1))
    previous = latest_completed_session(close_at - timedelta(minutes=1))

    def respond(request):
        phase["calls"] += 1
        rows = [{"date": previous.strftime("%m/%d/%Y"), "close": "50", "low": "49"}]
        if phase["published"]:
            rows.append({"date": completed.strftime("%m/%d/%Y"), "close": "51", "low": "50"})
        return httpx.Response(
            200, json={"status": {"rCode": 200}, "data": {"tradesTable": {"rows": rows}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = OptionChainService(client, history_cache=TickerCache(86400))

        async def chain(ticker):
            return OptionChainResponse(
                ticker=ticker,
                fetched_at=phase["now"],
                from_cache=False,
                last_trade=None,
                spot=Decimal(51),
                rows=[],
            )

        async def info(ticker, now):
            return StockInfoResponse(
                ticker=ticker,
                fetched_at=now,
                from_cache=False,
                bid=None,
                ask=None,
                quote_timestamp=None,
                is_real_time=False,
                market_session=None,
            )

        service.get_chain = chain
        service.get_info = info
        before = await loader(service, "IREN", phase["now"])
        assert positive_close(before.history.bars, previous) == Decimal(50)
        assert phase["calls"] == 1
        # The page fetch starts before close but finishes after it. Invalidate
        # with the refreshed clock; do not refetch within this request.
        after = close_at + timedelta(minutes=1)
        crossed = await loader(service, "IREN", phase["now"], clock=lambda: after)
        assert positive_close(crossed.history.bars, completed) is None
        assert phase["calls"] == 1
        phase.update(now=after, published=True)
        recovered = await loader(service, "IREN", after)
        assert positive_close(recovered.history.bars, completed) == Decimal(51)
        assert phase["calls"] == 2
        cached = await loader(service, "IREN", after)
        assert positive_close(cached.history.bars, completed) == Decimal(51)
        assert phase["calls"] == 2
