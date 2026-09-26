"""Bounded public inputs for market-implied option odds."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from options_api.models import OptionChainResponse, OptionQuote, normalize_ticker
from options_api.money import parse_decimal, usable_price
from options_api.parser import parse_nonnegative_int

_LOG = logging.getLogger("options_api.market_sources")
_NY = ZoneInfo("America/New_York")
_OCC = re.compile(r"^(?P<root>[A-Z0-9]{1,8})(?P<day>\d{6})(?P<side>[CP])(?P<strike>\d{8})$")
_TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
)
_TREASURY_TERMS = {
    "BC_1MONTH": 1 / 12,
    "BC_2MONTH": 2 / 12,
    "BC_3MONTH": 3 / 12,
    "BC_4MONTH": 4 / 12,
    "BC_6MONTH": 0.5,
    "BC_1YEAR": 1.0,
    "BC_2YEAR": 2.0,
    "BC_3YEAR": 3.0,
    "BC_5YEAR": 5.0,
    "BC_7YEAR": 7.0,
    "BC_10YEAR": 10.0,
    "BC_20YEAR": 20.0,
    "BC_30YEAR": 30.0,
}
_YAHOO_MAX_EXPIRIES = 64
_YAHOO_MAX_ROWS = 30_000
_YAHOO_DEADLINE_SECONDS = 75.0
_YAHOO_WORKERS = threading.BoundedSemaphore(2)
_DIVIDEND_WORKERS = threading.BoundedSemaphore(4)
_treasury_cache: tuple[datetime, TreasuryCurve] | None = None
_treasury_lock = asyncio.Lock()


@dataclass(frozen=True)
class TreasuryCurve:
    as_of: date
    points: tuple[tuple[float, float], ...]  # years, continuously compounded annual rate

    def rate_for(self, expiry: date, quote_time: datetime) -> float | None:
        quote_day = quote_time.astimezone(_NY).date()
        age = (quote_day - self.as_of).days
        days = (expiry - quote_day).days
        if age < 0 or age > 7 or days <= 0 or len(self.points) < 3:
            return None
        years = days / 365.25
        if years > self.points[-1][0]:
            return None
        if years <= self.points[0][0]:
            return self.points[0][1]
        for (t0, r0), (t1, r1) in zip(self.points, self.points[1:], strict=False):
            if years <= t1:
                return r0 + (r1 - r0) * (years - t0) / (t1 - t0)
        return None


@dataclass(frozen=True)
class DividendStatus:
    kind: Literal["nonpayer", "payer", "unknown"]
    as_of: datetime
    next_ex_date: date | None = None

    def eligible_for(self, expiry: date) -> tuple[bool, str | None]:
        if self.kind == "nonpayer":
            return True, None
        if self.kind == "payer" and self.next_ex_date is not None and expiry < self.next_ex_date:
            return True, None
        if self.kind == "payer":
            return False, "Dividend exposure is unsupported for this expiration"
        return False, "Dividend status could not be verified"


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].upper()


def parse_treasury_curve(xml: bytes, as_of: datetime) -> TreasuryCurve | None:
    if not xml or len(xml) > 2_000_000:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    latest: TreasuryCurve | None = None
    for entry in root.iter():
        if _tag_name(entry.tag) != "ENTRY":
            continue
        fields = {
            _tag_name(node.tag): (node.text or "").strip()
            for node in entry.iter()
            if len(node) == 0
        }
        raw_day = fields.get("NEW_DATE", "")[:10]
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        if day > as_of.astimezone(_NY).date():
            continue
        points: list[tuple[float, float]] = []
        for key, years in _TREASURY_TERMS.items():
            value = parse_decimal(fields.get(key))
            if value is None or value < Decimal("-5") or value > Decimal("50"):
                continue
            percent = float(value) / 100
            points.append((years, 2 * math.log1p(percent / 2)))
        if len(points) >= 3 and (latest is None or day > latest.as_of):
            latest = TreasuryCurve(day, tuple(sorted(points)))
    return latest


async def fetch_treasury_curve(
    client: httpx.AsyncClient, as_of: datetime | None = None
) -> TreasuryCurve | None:
    """Use the current Treasury month; hold a valid result for at most two hours."""
    global _treasury_cache
    now = as_of or datetime.now(UTC)
    async with _treasury_lock:
        if _treasury_cache is not None:
            fetched, curve = _treasury_cache
            if (
                now - fetched < timedelta(hours=2)
                and curve.rate_for(now.astimezone(_NY).date() + timedelta(days=1), now) is not None
            ):
                return curve
        month = now.astimezone(_NY).date().replace(day=1)
        for requested_month in (month, month - timedelta(days=1)):
            try:
                response = await client.get(
                    _TREASURY_URL,
                    params={
                        "data": "daily_treasury_yield_curve",
                        "field_tdr_date_value_month": requested_month.strftime("%Y%m"),
                    },
                    timeout=25.0,
                )
                response.raise_for_status()
                curve = parse_treasury_curve(response.content, now)
            except (httpx.HTTPError, ValueError):
                curve = None
            if curve is not None and 0 <= (now.astimezone(_NY).date() - curve.as_of).days <= 7:
                _treasury_cache = (now, curve)
                return curve
        if _treasury_cache is not None:
            curve = _treasury_cache[1]
            if 0 <= (now.astimezone(_NY).date() - curve.as_of).days <= 7:
                return curve
        return None


def _parse_ex_date(value: Any) -> date | None:
    try:
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return datetime.fromtimestamp(float(value), UTC).astimezone(_NY).date()
        if isinstance(value, str):
            return date.fromisoformat(value[:10])
    except (OverflowError, OSError, ValueError):
        pass
    return None


def _dividend_status_sync(ticker: str, now: datetime) -> DividendStatus:
    import yfinance as yf

    info = yf.Ticker(ticker).info
    if not isinstance(info, dict) or info.get("quoteType") != "EQUITY":
        return DividendStatus("unknown", now)
    rate = parse_decimal(info.get("dividendRate"))
    trailing = parse_decimal(info.get("trailingAnnualDividendRate"))
    if any(value is not None and value < 0 for value in (rate, trailing)):
        return DividendStatus("unknown", now)
    ex_date = _parse_ex_date(info.get("exDividendDate"))
    if any(value is not None and value > 0 for value in (rate, trailing)):
        return DividendStatus(
            "payer", now, ex_date if ex_date and ex_date > now.astimezone(_NY).date() else None
        )
    if (rate == 0 or trailing == 0) and ex_date is None:
        return DividendStatus("nonpayer", now)
    return DividendStatus("unknown", now)


async def fetch_dividend_status(ticker: str, as_of: datetime | None = None) -> DividendStatus:
    now = as_of or datetime.now(UTC)
    safe = normalize_ticker(ticker)
    if safe is None:
        return DividendStatus("unknown", now)

    def work() -> DividendStatus:
        if not _DIVIDEND_WORKERS.acquire(blocking=False):
            return DividendStatus("unknown", now)
        try:
            return _dividend_status_sync(safe, now)
        finally:
            _DIVIDEND_WORKERS.release()

    try:
        return await asyncio.wait_for(asyncio.to_thread(work), timeout=20)
    except Exception:
        _LOG.info("Yahoo dividend status unavailable for %s", safe)
        return DividendStatus("unknown", now)


def _yahoo_underlying(underlying: Any) -> tuple[Decimal | None, str | None]:
    if not isinstance(underlying, dict):
        return None, None
    # Match regularMarketPrice to regularMarketTime; bid/ask may be from after hours.
    spot = usable_price(parse_decimal(underlying.get("regularMarketPrice")))
    raw_time = underlying.get("regularMarketTime")
    try:
        quote_time = datetime.fromtimestamp(int(raw_time), UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        quote_time = None
    return spot, quote_time


def _parse_yahoo_contracts(
    ticker: str, expiry: str, calls: Any, puts: Any, total_rows: int
) -> tuple[list[OptionQuote], int]:
    if calls is None or puts is None:
        raise ValueError("Yahoo option side is missing")
    by_strike: dict[Decimal, dict[str, Any]] = {}
    raw_count = 0
    for side, frame in (("call", calls), ("put", puts)):
        raw_count += len(frame)
        if total_rows + raw_count > _YAHOO_MAX_ROWS:
            raise ValueError("Yahoo option row limit exceeded")
        for row in frame.to_dict("records"):
            symbol = row.get("contractSymbol")
            if not isinstance(symbol, str):
                raise ValueError("Yahoo contract symbol missing")
            match = _OCC.fullmatch(symbol.upper())
            if match is None:
                raise ValueError("Yahoo contract symbol invalid")
            root = match.group("root")
            if root != ticker or row.get("contractSize") != "REGULAR":
                continue  # Adjusted contracts cannot be represented as a standard watch.
            if row.get("currency") != "USD":
                raise ValueError("Yahoo contract currency is not USD")
            code = match.group("day")
            try:
                symbol_expiry = date(
                    2000 + int(code[:2]), int(code[2:4]), int(code[4:])
                ).isoformat()
            except ValueError as exc:
                raise ValueError("Yahoo contract expiration invalid") from exc
            strike = parse_decimal(row.get("strike"))
            symbol_strike = Decimal(match.group("strike")) / 1000
            if (
                symbol_expiry != expiry
                or usable_price(strike) is None
                or strike != symbol_strike
                or match.group("side") != side[0].upper()
            ):
                raise ValueError("Yahoo contract terms conflict with symbol")
            quote = by_strike.setdefault(strike, {})
            if side in quote:
                raise ValueError("Duplicate Yahoo contract")
            quote[side] = row
    rows: list[OptionQuote] = []
    for strike, sides in by_strike.items():
        call = sides.get("call", {})
        put = sides.get("put", {})
        rows.append(
            OptionQuote(
                ticker=ticker,
                expiration=expiry,
                strike=strike,
                root=ticker,
                identity_reason=None,
                call_bid=parse_decimal(call.get("bid")),
                call_ask=parse_decimal(call.get("ask")),
                call_volume=parse_nonnegative_int(call.get("volume")),
                call_open_interest=parse_nonnegative_int(call.get("openInterest")),
                put_bid=parse_decimal(put.get("bid")),
                put_ask=parse_decimal(put.get("ask")),
                put_volume=parse_nonnegative_int(put.get("volume")),
                put_open_interest=parse_nonnegative_int(put.get("openInterest")),
            )
        )
    if not rows:
        raise ValueError("Yahoo expiry has no standard contracts")
    return rows, raw_count


def _yahoo_chain_sync(
    ticker: str, required_expirations: frozenset[str], deadline: float
) -> OptionChainResponse:
    import yfinance as yf

    provider = yf.Ticker(ticker)
    expirations = tuple(provider.options)
    if (
        not expirations
        or len(expirations) > _YAHOO_MAX_EXPIRIES
        or len(set(expirations)) != len(expirations)
    ):
        raise ValueError("Yahoo expiration list is missing or exceeds the limit")
    if not required_expirations.issubset(expirations):
        raise ValueError("Yahoo expiration list omits a Nasdaq date")
    rows: list[OptionQuote] = []
    spot: Decimal | None = None
    quote_time: str | None = None
    count = 0
    for expiry in expirations:
        if time.monotonic() >= deadline:
            raise TimeoutError("Yahoo option snapshot deadline exceeded")
        date.fromisoformat(expiry)
        chain = provider.option_chain(expiry)
        expiry_rows, raw_count = _parse_yahoo_contracts(
            ticker, expiry, chain.calls, chain.puts, count
        )
        count += raw_count
        rows.extend(expiry_rows)
        page_spot, page_time = _yahoo_underlying(chain.underlying)
        if spot is None:
            spot, quote_time = page_spot, page_time
        elif page_spot is not None and abs(page_spot / spot - 1) > Decimal("0.02"):
            raise ValueError("Yahoo underlying changed during option snapshot")
        if quote_time and page_time:
            drift = abs(
                (
                    datetime.fromisoformat(page_time) - datetime.fromisoformat(quote_time)
                ).total_seconds()
            )
            if drift > 120:
                raise ValueError("Yahoo option pages span multiple quote times")
    rows.sort(key=lambda row: (row.expiration, row.strike))
    if len(rows) > _YAHOO_MAX_ROWS:
        raise ValueError("Yahoo standard contract row limit exceeded")
    if spot is None or quote_time is None:
        raise ValueError("Yahoo replacement lacks a dated underlying price")
    last_trade = f"${spot} (AS OF {quote_time})"
    return OptionChainResponse(
        ticker=ticker,
        fetched_at=datetime.now(UTC),
        from_cache=False,
        last_trade=last_trade,
        last_trade_timestamp=quote_time,
        spot=spot,
        source="yahoo",
        truncated=False,
        options_available=True,
        rows=rows,
    )


async def fetch_yahoo_chain(
    ticker: str, required_expirations: frozenset[str] = frozenset()
) -> OptionChainResponse | None:
    """Return an all-expiry replacement, or None so the caller keeps Nasdaq."""
    safe = normalize_ticker(ticker)
    if safe is None:
        return None
    deadline = time.monotonic() + _YAHOO_DEADLINE_SECONDS

    def work() -> OptionChainResponse | None:
        if not _YAHOO_WORKERS.acquire(blocking=False):
            return None
        try:
            return _yahoo_chain_sync(safe, required_expirations, deadline)
        finally:
            _YAHOO_WORKERS.release()

    try:
        return await asyncio.wait_for(asyncio.to_thread(work), timeout=_YAHOO_DEADLINE_SECONDS)
    except Exception as exc:
        _LOG.info("Yahoo chain replacement incomplete for %s: %s", safe, exc)
        return None
