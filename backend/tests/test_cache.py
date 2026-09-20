import asyncio

import pytest

from options_api.cache import TickerCache


async def test_cache_hit_within_ttl_skips_fetch() -> None:
    clock = {"now": 100.0}
    cache: TickerCache = TickerCache(ttl_seconds=30, monotonic=lambda: clock["now"])
    calls = {"n": 0}

    async def fetch() -> str:
        calls["n"] += 1
        return f"value-{calls['n']}"

    value, from_cache = await cache.get_or_fetch("IREN", fetch)
    assert value == "value-1"
    assert from_cache is False
    assert calls["n"] == 1

    clock["now"] = 129.9
    value, from_cache = await cache.get_or_fetch("IREN", fetch)
    assert value == "value-1"
    assert from_cache is True
    assert calls["n"] == 1


async def test_cache_hit_preserves_none_values() -> None:
    cache: TickerCache = TickerCache(ttl_seconds=30)
    calls = 0

    async def fetch() -> None:
        nonlocal calls
        calls += 1
        return None

    assert await cache.get_or_fetch("sec:IREN", fetch) == (None, False)
    assert await cache.get_or_fetch("sec:IREN", fetch) == (None, True)
    assert calls == 1


async def test_cache_expires_after_ttl() -> None:
    clock = {"now": 0.0}
    cache: TickerCache = TickerCache(ttl_seconds=30, monotonic=lambda: clock["now"])
    calls = {"n": 0}

    async def fetch() -> str:
        calls["n"] += 1
        return f"value-{calls['n']}"

    await cache.get_or_fetch("IREN", fetch)
    clock["now"] = 30.0
    value, from_cache = await cache.get_or_fetch("IREN", fetch)
    assert value == "value-2"
    assert from_cache is False
    assert calls["n"] == 2


async def test_cache_is_per_ticker() -> None:
    cache: TickerCache = TickerCache(ttl_seconds=30)
    calls: list[str] = []

    async def fetch_iren() -> str:
        calls.append("IREN")
        return "iren"

    async def fetch_cifr() -> str:
        calls.append("CIFR")
        return "cifr"

    await cache.get_or_fetch("IREN", fetch_iren)
    await cache.get_or_fetch("CIFR", fetch_cifr)
    assert calls == ["IREN", "CIFR"]


async def test_single_flight_shares_one_fetch() -> None:
    cache: TickerCache = TickerCache(ttl_seconds=30)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def fetch() -> str:
        calls["n"] += 1
        started.set()
        await release.wait()
        return "shared"

    first = asyncio.create_task(cache.get_or_fetch("IREN", fetch))
    await started.wait()
    second = asyncio.create_task(cache.get_or_fetch("IREN", fetch))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert [value for value, _ in results] == ["shared", "shared"]
    assert [from_cache for _, from_cache in results] == [False, True]
    assert calls["n"] == 1
    assert cache._inflight == {}


async def test_cache_churn_prunes_every_expired_key() -> None:
    clock = {"now": 0.0}
    cache: TickerCache = TickerCache(
        ttl_seconds=2 * 86_400,
        monotonic=lambda: clock["now"],
    )

    for day in range(365):
        clock["now"] = day * 86_400

        async def fetch(day: int = day) -> int:
            return day

        value, from_cache = await cache.get_or_fetch(f"history-{day}", fetch)
        assert value == day
        assert from_cache is False
        assert len(cache._entries) <= 2
        assert cache._inflight == {}

    assert set(cache._entries) == {"history-363", "history-364"}


async def test_failed_fetch_is_removed_and_retryable() -> None:
    cache: TickerCache = TickerCache()
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")
        return "recovered"

    with pytest.raises(RuntimeError, match="temporary failure"):
        await cache.get_or_fetch("IREN", fetch)
    assert cache._entries == {}
    assert cache._inflight == {}

    assert await cache.get_or_fetch("IREN", fetch) == ("recovered", False)
    assert calls == 2


async def test_cancelled_creator_does_not_cancel_or_strand_shared_fetch() -> None:
    cache: TickerCache = TickerCache()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "shared"

    creator = asyncio.create_task(cache.get_or_fetch("IREN", fetch))
    await started.wait()
    joiner = asyncio.create_task(cache.get_or_fetch("IREN", fetch))
    await asyncio.sleep(0)
    creator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creator
    release.set()

    assert await joiner == ("shared", True)
    assert calls == 1
    assert cache._entries["IREN"][1] == "shared"
    assert cache._inflight == {}


async def test_concurrent_keys_each_have_one_active_fetch() -> None:
    cache: TickerCache = TickerCache()
    calls: dict[str, int] = {}

    def fetch_for(key: str):
        async def fetch() -> str:
            calls[key] = calls.get(key, 0) + 1
            await asyncio.sleep(0)
            return key

        return fetch

    results = await asyncio.gather(
        *[
            cache.get_or_fetch(key, fetch_for(key))
            for key in ("IREN", "IREN", "CIFR", "CIFR", "WULF", "WULF")
        ]
    )

    assert [value for value, _ in results] == [
        "IREN",
        "IREN",
        "CIFR",
        "CIFR",
        "WULF",
        "WULF",
    ]
    assert calls == {"IREN": 1, "CIFR": 1, "WULF": 1}
    assert cache._inflight == {}


async def test_max_entries_evicts_oldest_after_expired() -> None:
    clock = {"now": 0.0}
    cache: TickerCache = TickerCache(
        ttl_seconds=1000,
        monotonic=lambda: clock["now"],
        max_entries=2,
    )

    async def fetch_a() -> str:
        return "a"

    async def fetch_b() -> str:
        return "b"

    async def fetch_c() -> str:
        return "c"

    await cache.get_or_fetch("A", fetch_a)
    clock["now"] = 1
    await cache.get_or_fetch("B", fetch_b)
    clock["now"] = 2
    await cache.get_or_fetch("C", fetch_c)
    assert set(cache._entries) == {"B", "C"}


async def test_max_entries_prunes_expired_before_oldest_live() -> None:
    clock = {"now": 0.0}
    cache: TickerCache = TickerCache(
        ttl_seconds=10,
        monotonic=lambda: clock["now"],
        max_entries=2,
    )

    async def fetch_a() -> str:
        return "a"

    async def fetch_b() -> str:
        return "b"

    async def fetch_c() -> str:
        return "c"

    await cache.get_or_fetch("A", fetch_a)
    clock["now"] = 5
    await cache.get_or_fetch("B", fetch_b)
    clock["now"] = 11
    await cache.get_or_fetch("C", fetch_c)
    assert set(cache._entries) == {"B", "C"}
