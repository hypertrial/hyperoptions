from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from options_api.models import Ticker, normalize_ticker

NASDAQ_OPTION_CHAIN_URL = "https://api.nasdaq.com/api/quote/{ticker}/option-chain"
NASDAQ_INFO_URL = "https://api.nasdaq.com/api/quote/{ticker}/info"
NASDAQ_HISTORICAL_URL = "https://api.nasdaq.com/api/quote/{ticker}/historical"
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"

NASDAQ_PARAMS = {
    "assetclass": "stocks",
    "limit": "5000",
    "fromdate": "all",
    "money": "all",
    "type": "all",
    "callput": "callput",
}

NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

logger = logging.getLogger("options_api.nasdaq")


@dataclass(frozen=True)
class FetchPolicy:
    name: str
    connect_timeout: float
    read_timeout: float
    write_timeout: float
    pool_timeout: float
    deadline_seconds: float
    max_attempts: int
    backoff_seconds: float

    def __post_init__(self) -> None:
        if self.max_attempts not in {1, 2}:
            raise ValueError("max_attempts must be 1 or 2")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")

    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.write_timeout,
            pool=self.pool_timeout,
        )


CHAIN_POLICY = FetchPolicy(
    name="chain",
    connect_timeout=5.0,
    read_timeout=15.0,
    write_timeout=10.0,
    pool_timeout=5.0,
    deadline_seconds=22.0,
    max_attempts=2,
    backoff_seconds=0.2,
)

CONTEXT_POLICY = FetchPolicy(
    name="context",
    connect_timeout=3.0,
    read_timeout=5.0,
    write_timeout=5.0,
    pool_timeout=5.0,
    deadline_seconds=8.0,
    max_attempts=2,
    backoff_seconds=0.1,
)

HTTP_TIMEOUT = CHAIN_POLICY.timeout()


NasdaqKind = Literal[
    "invalid", "not_found", "rate_limited", "unavailable", "timeout", "malformed"
]


class NasdaqError(Exception):
    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        kind: NasdaqKind,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.kind: NasdaqKind = kind
        self.retry_after = retry_after

    @classmethod
    def invalid(cls) -> NasdaqError:
        return cls(400, "Invalid ticker", kind="invalid")

    @classmethod
    def not_found(cls) -> NasdaqError:
        return cls(404, "Symbol not exists", kind="not_found")

    @classmethod
    def rate_limited(cls, retry_after: str | None = None) -> NasdaqError:
        return cls(503, "Nasdaq unavailable", kind="rate_limited", retry_after=retry_after)

    @classmethod
    def unavailable(cls) -> NasdaqError:
        return cls(502, "Nasdaq unavailable", kind="unavailable")

    @classmethod
    def timeout(cls) -> NasdaqError:
        return cls(504, "Nasdaq timeout", kind="timeout")

    @classmethod
    def malformed(cls) -> NasdaqError:
        return cls(502, "Nasdaq response is malformed", kind="malformed")


def _retry_after(response: httpx.Response) -> str | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit() and 1 <= len(text) <= 5:
        return text
    return None


def _map_http_error(response: httpx.Response) -> NasdaqError:
    if response.status_code == 429:
        return NasdaqError.rate_limited(_retry_after(response))
    return NasdaqError.unavailable()


def _log(operation: str, attempt: int, started: float, result: str) -> None:
    logger.info(
        "nasdaq_fetch operation=%s attempt=%s elapsed_ms=%s result=%s",
        operation,
        attempt,
        int((time.monotonic() - started) * 1000),
        result,
    )


def _request_timeout(policy: FetchPolicy, started: float) -> httpx.Timeout:
    remaining = policy.deadline_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise NasdaqError.timeout()
    base = policy.timeout()
    return httpx.Timeout(
        connect=min(base.connect or remaining, remaining),
        read=min(base.read or remaining, remaining),
        write=min(base.write or remaining, remaining),
        pool=min(base.pool or remaining, remaining),
    )


async def _fetch_payload(
    url: str,
    client: httpx.AsyncClient,
    params: dict[str, str],
    policy: FetchPolicy,
    operation: str,
) -> dict[str, Any]:
    try:
        async with asyncio.timeout(policy.deadline_seconds):
            return await _fetch_payload_with_retries(
                url, client, params, policy, operation
            )
    except TimeoutError as exc:
        raise NasdaqError.timeout() from exc


async def _fetch_payload_with_retries(
    url: str,
    client: httpx.AsyncClient,
    params: dict[str, str],
    policy: FetchPolicy,
    operation: str,
) -> dict[str, Any]:
    started = time.monotonic()
    for attempt in range(1, policy.max_attempts + 1):
        if time.monotonic() - started >= policy.deadline_seconds:
            _log(operation, attempt, started, "deadline")
            raise NasdaqError.timeout()
        try:
            timeout = _request_timeout(policy, started)
        except NasdaqError:
            _log(operation, attempt, started, "deadline")
            raise
        try:
            response = await client.get(
                url,
                params=params,
                headers=NASDAQ_HEADERS,
                timeout=timeout,
            )
        except httpx.RequestError as exc:
            timed_out = isinstance(exc, httpx.TimeoutException)
            _log(operation, attempt, started, "timeout" if timed_out else "request_error")
            if attempt >= policy.max_attempts:
                if timed_out:
                    raise NasdaqError.timeout() from exc
                raise NasdaqError.unavailable() from exc
            await _backoff(policy, started)
            continue
        if response.status_code == 429:
            _log(operation, attempt, started, "429")
            raise _map_http_error(response)
        if response.status_code >= 400:
            _log(operation, attempt, started, f"http_{response.status_code}")
            if response.status_code == 400:
                missing = _symbol_not_exists_payload(response)
                if missing is not None:
                    raise missing
            raise _map_http_error(response)
        if not response.content:
            raise NasdaqError.unavailable()
        try:
            payload = response.json()
        except ValueError as exc:
            _log(operation, attempt, started, "invalid_json")
            raise NasdaqError.unavailable() from exc
        if not isinstance(payload, dict):
            raise NasdaqError.malformed()
        _log(operation, attempt, started, "ok")
        return payload
    raise NasdaqError.timeout()


async def _backoff(policy: FetchPolicy, started: float) -> None:
    remaining = policy.deadline_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise NasdaqError.timeout()
    delay = min(policy.backoff_seconds, remaining)
    if delay > 0:
        await asyncio.sleep(delay)


def require_ticker(ticker: Ticker) -> str:
    normalized = normalize_ticker(ticker)
    if normalized is None:
        raise NasdaqError.invalid()
    return normalized


def _symbol_not_exists_payload(response: httpx.Response) -> NasdaqError | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if symbol_not_exists(payload):
        return NasdaqError.not_found()
    return None


def symbol_not_exists(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    status = payload.get("status")
    if not isinstance(status, dict) or status.get("rCode") != 400:
        return False
    messages = status.get("bCodeMessage")
    if not isinstance(messages, list):
        return False
    for item in messages:
        if not isinstance(item, dict):
            continue
        text = str(item.get("errorMessage") or "").strip().lower()
        if "symbol not exists" in text:
            return True
    return False


async def fetch_option_chain_payload(
    ticker: Ticker, client: httpx.AsyncClient
) -> dict[str, Any]:
    safe = require_ticker(ticker)
    return await _fetch_payload(
        NASDAQ_OPTION_CHAIN_URL.format(ticker=safe),
        client,
        NASDAQ_PARAMS,
        CHAIN_POLICY,
        "chain",
    )


async def fetch_stock_info_payload(
    ticker: Ticker, client: httpx.AsyncClient
) -> dict[str, Any]:
    safe = require_ticker(ticker)
    return await _fetch_payload(
        NASDAQ_INFO_URL.format(ticker=safe),
        client,
        {"assetclass": "stocks"},
        CONTEXT_POLICY,
        "info",
    )


async def fetch_historical_payload(
    ticker: Ticker,
    client: httpx.AsyncClient,
    from_date: str,
) -> dict[str, Any]:
    safe = require_ticker(ticker)
    return await _fetch_payload(
        NASDAQ_HISTORICAL_URL.format(ticker=safe),
        client,
        {"assetclass": "stocks", "fromdate": from_date, "limit": "5000"},
        CONTEXT_POLICY,
        "history",
    )


async def fetch_screener_payload(client: httpx.AsyncClient) -> dict[str, Any]:
    return await _fetch_payload(
        NASDAQ_SCREENER_URL,
        client,
        {"exchange": "nasdaq", "download": "true"},
        CHAIN_POLICY,
        "screener",
    )


def create_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, http2=True, follow_redirects=True)
