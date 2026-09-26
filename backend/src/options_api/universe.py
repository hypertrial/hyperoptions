from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import httpx

from options_api.models import TickerListing, normalize_ticker
from options_api.nasdaq import NasdaqError, fetch_screener_payload
from options_api.parser import parse_screener_listings

UNIVERSE_TTL_SECONDS = 86_400.0
NON_EQUITY_NAME = re.compile(r"\b(?:warrants?|rights?|units?|preferred|notes?|bonds?)\b", re.I)


class TickerUniverse:
    """Dedicated 24h Nasdaq-listed universe with stale-on-error refresh."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._monotonic = monotonic or time.monotonic
        self._listings: tuple[TickerListing, ...] = ()
        self._by_symbol: dict[str, TickerListing] = {}
        self._as_of: datetime | None = None
        self._stored_at: float | None = None
        self._inflight: asyncio.Task[None] | None = None
        self._guard = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(self._by_symbol)

    @property
    def as_of(self) -> datetime | None:
        return self._as_of

    @property
    def listings(self) -> tuple[TickerListing, ...]:
        return self._listings

    def forecast_peer_listings(self) -> list[TickerListing]:
        return [
            item
            for item in self._listings
            if item.sector and item.sector.strip() and not NON_EQUITY_NAME.search(item.name)
        ]

    def seed(self, listings: Sequence[TickerListing], as_of: datetime | None = None) -> None:
        kept = [item for item in listings if normalize_ticker(item.symbol) == item.symbol]
        kept.sort(key=lambda item: item.symbol)
        self._listings = tuple(kept)
        self._by_symbol = {item.symbol: item for item in kept}
        self._as_of = as_of or datetime.now(UTC)
        self._stored_at = self._monotonic()

    def contains(self, symbol: str) -> bool:
        return symbol in self._by_symbol

    def listing(self, symbol: str) -> TickerListing | None:
        return self._by_symbol.get(symbol)

    def search(self, query: str, limit: int) -> list[TickerListing]:
        needle = query.strip().upper()
        if not self._listings or limit <= 0:
            return []
        if not needle:
            return list(self._listings[:limit])
        prefix: list[TickerListing] = []
        named: list[TickerListing] = []
        for item in self._listings:
            if item.symbol.startswith(needle):
                prefix.append(item)
            elif needle in item.name.upper():
                named.append(item)
        return (prefix + named)[:limit]

    async def close(self) -> None:
        async with self._guard:
            task = self._inflight
            self._inflight = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def ensure(self) -> bool:
        if self._fresh():
            return True
        async with self._guard:
            if self._fresh():
                return True
            task = self._inflight
            if task is None:
                task = asyncio.create_task(self._refresh_and_clear())
                self._inflight = task
        await asyncio.shield(task)
        return self.available

    async def _refresh_and_clear(self) -> None:
        task = asyncio.current_task()
        try:
            await self._refresh()
        finally:
            async with self._guard:
                if self._inflight is task:
                    self._inflight = None

    def _fresh(self) -> bool:
        if not self.available or self._stored_at is None:
            return False
        return self._monotonic() - self._stored_at < UNIVERSE_TTL_SECONDS

    async def _refresh(self) -> None:
        try:
            payload = await fetch_screener_payload(self._client)
            listings = parse_screener_listings(payload)
        except (NasdaqError, ValueError):
            return
        if listings:
            self.seed(listings)
