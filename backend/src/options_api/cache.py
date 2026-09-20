from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
_MISSING = object()


class TickerCache:
    """In-memory per-ticker TTL cache with single-flight fetches."""

    def __init__(
        self,
        ttl_seconds: float = 30.0,
        monotonic: Callable[[], float] | None = None,
        max_entries: int | None = 64,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._monotonic = monotonic or time.monotonic
        self._entries: dict[str, tuple[float, object]] = {}
        self._inflight: dict[str, asyncio.Task[object]] = {}
        self._guard = asyncio.Lock()

    def _prune(self) -> None:
        now = self._monotonic()
        expired = [
            ticker
            for ticker, (stored_at, _) in self._entries.items()
            if now - stored_at >= self.ttl_seconds
        ]
        for ticker in expired:
            self._entries.pop(ticker, None)

    def _evict_if_needed(self) -> None:
        self._prune()
        if self.max_entries is None or len(self._entries) < self.max_entries:
            return
        overflow = len(self._entries) - self.max_entries + 1
        oldest = sorted(self._entries.items(), key=lambda item: item[1][0])
        for key, _ in oldest[:overflow]:
            self._entries.pop(key, None)

    def _fresh(self, ticker: str) -> object:
        self._prune()
        entry = self._entries.get(ticker)
        if entry is None:
            return _MISSING
        return entry[1]

    async def _fetch_and_store(
        self, ticker: str, fetch: Callable[[], Awaitable[T]]
    ) -> T:
        task = asyncio.current_task()
        try:
            value = await fetch()
            self._evict_if_needed()
            self._entries[ticker] = (self._monotonic(), value)
            return value
        finally:
            async with self._guard:
                if self._inflight.get(ticker) is task:
                    self._inflight.pop(ticker, None)

    @staticmethod
    def _consume_exception(task: asyncio.Task[object]) -> None:
        if not task.cancelled():
            task.exception()

    async def get_or_fetch(self, ticker: str, fetch: Callable[[], Awaitable[T]]) -> tuple[T, bool]:
        cached = self._fresh(ticker)
        if cached is not _MISSING:
            return cached, True  # type: ignore[return-value]
        async with self._guard:
            cached = self._fresh(ticker)
            if cached is not _MISSING:
                return cached, True  # type: ignore[return-value]
            task = self._inflight.get(ticker)
            creator = task is None
            if task is None:
                task = asyncio.create_task(self._fetch_and_store(ticker, fetch))
                self._inflight[ticker] = task
                task.add_done_callback(self._consume_exception)
        value = await asyncio.shield(task)
        return value, not creator  # type: ignore[return-value]

    def discard(self, key: str) -> None:
        self._prune()
        self._entries.pop(key, None)
