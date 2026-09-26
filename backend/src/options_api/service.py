from __future__ import annotations

from datetime import UTC, datetime

import httpx

from options_api.cache import TickerCache
from options_api.memo import ContractMemo
from options_api.models import (
    HistoricalResponse,
    OptionChainResponse,
    StockInfoResponse,
    Ticker,
)
from options_api.nasdaq import (
    NasdaqError,
    fetch_historical_payload,
    fetch_option_chain_payload,
    fetch_stock_info_payload,
    symbol_not_exists,
)
from options_api.parser import (
    extract_last_trade_price,
    extract_last_trade_timestamp,
    nasdaq_status_ok,
    parse_historical_bars,
    parse_option_chain,
    parse_stock_info,
)


class OptionChainService:
    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: TickerCache | None = None,
        info_cache: TickerCache | None = None,
        history_cache: TickerCache | None = None,
        memo: ContractMemo | None = None,
    ) -> None:
        self._client = client
        self.memo = memo or ContractMemo()
        self._chain_cache = cache or TickerCache(ttl_seconds=30.0)
        self._info_cache = info_cache or TickerCache(ttl_seconds=30.0)
        self._history_cache = history_cache or TickerCache(ttl_seconds=86_400.0)

    async def get_chain(self, ticker: Ticker) -> OptionChainResponse:
        async def fetch() -> OptionChainResponse:
            payload = await fetch_option_chain_payload(ticker, self._client)
            if symbol_not_exists(payload):
                raise NasdaqError.not_found()
            if not nasdaq_status_ok(payload):
                raise NasdaqError.malformed()
            try:
                rows, truncated, last_trade, options_available = parse_option_chain(ticker, payload)
            except ValueError as exc:
                raise NasdaqError.malformed() from exc
            if not rows and options_available:
                raise NasdaqError.malformed()
            return OptionChainResponse(
                ticker=ticker,
                fetched_at=datetime.now(UTC),
                from_cache=False,
                last_trade=last_trade,
                last_trade_timestamp=extract_last_trade_timestamp(last_trade),
                spot=extract_last_trade_price(last_trade),
                truncated=truncated,
                options_available=options_available,
                rows=rows,
            )

        response, from_cache = await self._chain_cache.get_or_fetch(ticker, fetch)
        return response.model_copy(update={"from_cache": True}) if from_cache else response

    async def get_current_chain(self, ticker: Ticker) -> OptionChainResponse:
        """Recheck a selected contract against the provider at watch creation."""
        self._chain_cache.discard(ticker)
        return await self.get_chain(ticker)

    async def get_info(
        self, ticker: Ticker, scan_time: datetime | None = None
    ) -> StockInfoResponse:
        async def fetch() -> StockInfoResponse:
            payload = await fetch_stock_info_payload(ticker, self._client)
            if not nasdaq_status_ok(payload):
                raise NasdaqError.malformed()
            try:
                return parse_stock_info(ticker, payload, scan_time or datetime.now(UTC))
            except ValueError as exc:
                raise NasdaqError.malformed() from exc

        response, from_cache = await self._info_cache.get_or_fetch(ticker, fetch)
        return response.model_copy(update={"from_cache": True}) if from_cache else response

    async def get_history(self, ticker: Ticker, from_date: str) -> HistoricalResponse:
        key = f"{ticker}:{from_date}"

        async def fetch() -> HistoricalResponse:
            payload = await fetch_historical_payload(ticker, self._client, from_date)
            if not nasdaq_status_ok(payload):
                raise NasdaqError.malformed()
            try:
                bars = parse_historical_bars(payload)
            except ValueError as exc:
                raise NasdaqError.malformed() from exc
            return HistoricalResponse(
                ticker=ticker,
                fetched_at=datetime.now(UTC),
                from_cache=False,
                bars=bars,
            )

        response, from_cache = await self._history_cache.get_or_fetch(key, fetch)
        if not response.bars:
            self._history_cache.discard(key)
        return response.model_copy(update={"from_cache": True}) if from_cache else response
