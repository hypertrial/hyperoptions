from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

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


RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.RequestError,
)
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset()


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
    retryable_exceptions: tuple[type[BaseException], ...]
    retryable_status_codes: frozenset[int]

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
    retryable_exceptions=RETRYABLE_EXCEPTIONS,
    retryable_status_codes=RETRYABLE_STATUS_CODES,
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
    retryable_exceptions=RETRYABLE_EXCEPTIONS,
    retryable_status_codes=RETRYABLE_STATUS_CODES,
)

HTTP_TIMEOUT = CHAIN_POLICY.timeout()


class NasdaqError(Exception):
    def __init__(
        self, status_code: int, detail: str, retry_after: str | None = None
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


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
        return NasdaqError(
            503, "Nasdaq unavailable", retry_after=_retry_after(response)
        )
    return NasdaqError(502, "Nasdaq unavailable")


def _retryable_exception(exc: BaseException, policy: FetchPolicy) -> bool:
    return isinstance(exc, policy.retryable_exceptions)


def _retryable_status(status_code: int, policy: FetchPolicy) -> bool:
    return status_code in policy.retryable_status_codes


def _request_timeout(policy: FetchPolicy, started: float) -> httpx.Timeout:
    remaining = policy.deadline_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise NasdaqError(504, "Nasdaq timeout")
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
        raise NasdaqError(504, "Nasdaq timeout") from exc


async def _fetch_payload_with_retries(
    url: str,
    client: httpx.AsyncClient,
    params: dict[str, str],
    policy: FetchPolicy,
    operation: str,
) -> dict[str, Any]:
    started = time.monotonic()
    last_timeout: Exception | None = None
    attempts = min(policy.max_attempts, 2)
    for attempt in range(attempts):
        elapsed = time.monotonic() - started
        if elapsed >= policy.deadline_seconds:
            logger.info(
                "nasdaq_fetch operation=%s attempt=%s elapsed_ms=%s result=deadline",
                operation,
                attempt + 1,
                int(elapsed * 1000),
            )
            raise NasdaqError(504, "Nasdaq timeout")
        try:
            response = await client.get(
                url,
                params=params,
                headers=NASDAQ_HEADERS,
                timeout=_request_timeout(policy, started),
            )
        except httpx.TimeoutException as exc:
            last_timeout = exc
            logger.info(
                "nasdaq_fetch operation=%s attempt=%s elapsed_ms=%s result=timeout",
                operation,
                attempt + 1,
                int((time.monotonic() - started) * 1000),
            )
            if not _retryable_exception(exc, policy) or attempt + 1 >= attempts:
                raise NasdaqError(504, "Nasdaq timeout") from last_timeout
            await _backoff(policy, started)
            continue
        except httpx.RequestError as exc:
            last_timeout = exc
            logger.info(
                "nasdaq_fetch operation=%s attempt=%s elapsed_ms=%s result=request_error",
                operation,
                attempt + 1,
                int((time.monotonic() - started) * 1000),
            )
            if not _retryable_exception(exc, policy) or attempt + 1 >= attempts:
                raise NasdaqError(502, "Nasdaq unavailable") from last_timeout
            await _backoff(policy, started)
            continue
        if response.status_code == 429:
            logger.info(
                "nasdaq_fetch operation=%s attempt=%s elapsed_ms=%s result=429",
                operation,
                attempt + 1,
                int((time.monotonic() - started) * 1000),
            )
            raise _map_http_error(response)
        if response.status_code >= 400:
            logger.info(
                "nasdaq_fetch operation=%s attempt=%s elapsed_ms=%s result=http_%s",
                operation,
                attempt + 1,
                int((time.monotonic() - started) * 1000),
                response.status_code,
            )
            if response.status_code == 400:
                missing = _symbol_not_exists_payload(response)
                if missing is not None:
                    raise missing
            if _retryable_status(response.status_code, policy) and attempt + 1 < attempts:
                await _backoff(policy, started)
                continue
            raise _map_http_error(response)
        if not response.content:
            raise NasdaqError(502, "Nasdaq unavailable")
        try:
            payload = response.json()
        except ValueError as exc:
            logger.info(
                "nasdaq_fetch operation=%s attempt=%s elapsed_ms=%s result=invalid_json",
                operation,
                attempt + 1,
                int((time.monotonic() - started) * 1000),
            )
            raise NasdaqError(502, "Nasdaq unavailable") from exc
        if not isinstance(payload, dict):
            raise NasdaqError(502, "Nasdaq response is malformed")
        logger.info(
            "nasdaq_fetch operation=%s attempt=%s elapsed_ms=%s result=ok",
            operation,
            attempt + 1,
            int((time.monotonic() - started) * 1000),
        )
        return payload
    if last_timeout is not None and _retryable_exception(last_timeout, policy):
        if isinstance(last_timeout, httpx.TimeoutException):
            raise NasdaqError(504, "Nasdaq timeout") from last_timeout
        raise NasdaqError(502, "Nasdaq unavailable") from last_timeout
    raise NasdaqError(504, "Nasdaq timeout")


async def _backoff(policy: FetchPolicy, started: float) -> None:
    remaining = policy.deadline_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise NasdaqError(504, "Nasdaq timeout")
    delay = min(policy.backoff_seconds, remaining)
    if delay > 0:
        await asyncio.sleep(delay)


def require_ticker(ticker: Ticker) -> str:
    normalized = normalize_ticker(ticker)
    if normalized is None:
        raise NasdaqError(400, "Invalid ticker")
    return normalized


def _symbol_not_exists_payload(response: httpx.Response) -> NasdaqError | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if symbol_not_exists(payload):
        return NasdaqError(404, "Symbol not exists")
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
