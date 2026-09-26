from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from options_api.contract_identity import row_identity
from options_api.models import (
    HistoricalBar,
    OptionQuote,
    StockInfoResponse,
    Ticker,
    TickerListing,
    normalize_ticker,
)
from options_api.money import parse_decimal, usable_price

_LAST_TRADE_PRICE_RE = re.compile(r"\$([0-9,]+(?:\.[0-9]+)?)")
_LAST_TRADE_TIMESTAMP_RE = re.compile(r"\(AS OF (.+)\)\s*$", re.IGNORECASE)

_EXPIRATION_RE = re.compile(r"--(\d{6})", re.IGNORECASE)
_NASDAQ_ROW_LIMIT = 5000


def parse_price(value: Any) -> Decimal | None:
    return parse_decimal(value)


def parse_nonnegative_int(value: Any) -> int | None:
    number = parse_decimal(value)
    if number is None or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def parse_expiration(drill_down_url: Any) -> str | None:
    if not drill_down_url or not isinstance(drill_down_url, str):
        return None
    match = _EXPIRATION_RE.search(drill_down_url)
    if not match:
        return None
    yymmdd = match.group(1)
    year = 2000 + int(yymmdd[:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_expiry_group(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _as_mapping(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def nasdaq_status_ok(payload: Any) -> bool:
    root = _as_mapping(payload)
    if root is None:
        return False
    status = _as_mapping(root.get("status"))
    if status is None:
        return False
    return status.get("rCode") == 200


def options_unavailable(payload: Any) -> bool:
    root = _as_mapping(payload)
    if root is None:
        return False
    message = str(root.get("message") or "").strip().lower()
    if "options are not available" not in message:
        return False
    data = _as_mapping(root.get("data"))
    if data is None:
        return True
    table = _as_mapping(data.get("table"))
    rows = table.get("rows") if table else None
    return rows is None


def extract_table_rows(payload: Any) -> list[Any] | None:
    root = _as_mapping(payload)
    if root is None:
        return None
    data = _as_mapping(root.get("data"))
    if data is None:
        return None
    table = _as_mapping(data.get("table"))
    if table is None:
        return None
    rows = table.get("rows")
    if not isinstance(rows, list):
        return None
    return rows


def extract_last_trade_price(last_trade: str | None) -> Decimal | None:
    if not last_trade:
        return None
    match = _LAST_TRADE_PRICE_RE.search(last_trade)
    if not match:
        return None
    return parse_price(match.group(1))


def extract_last_trade_timestamp(last_trade: str | None) -> str | None:
    if not last_trade:
        return None
    match = _LAST_TRADE_TIMESTAMP_RE.search(last_trade)
    return match.group(1).strip() if match else None


def extract_last_trade(payload: Any) -> str | None:
    root = _as_mapping(payload)
    if root is None:
        return None
    data = _as_mapping(root.get("data"))
    if data is None:
        return None
    last_trade = data.get("lastTrade")
    if last_trade is None:
        return None
    text = str(last_trade).strip()
    return text or None


def parse_option_chain(
    ticker: Ticker, payload: Any
) -> tuple[list[OptionQuote], bool, str | None, bool]:
    if options_unavailable(payload):
        return [], False, extract_last_trade(payload), False
    rows = extract_table_rows(payload)
    if rows is None:
        raise ValueError("Nasdaq response is missing table.rows")
    truncated = len(rows) >= _NASDAQ_ROW_LIMIT
    quotes: list[OptionQuote] = []
    current_expiration: str | None = None
    for row in rows:
        mapping = _as_mapping(row)
        if mapping is None:
            continue
        raw_group = mapping.get("expirygroup")
        if _clean_text(raw_group) is not None:
            current_expiration = parse_expiry_group(raw_group)
        quote = _parse_row(ticker, mapping, current_expiration)
        if quote is not None:
            quotes.append(quote)
    return quotes, truncated, extract_last_trade(payload), True


def _parse_row(
    ticker: Ticker,
    mapping: dict[str, Any],
    fallback_expiration: str | None,
) -> OptionQuote | None:
    strike = parse_price(mapping.get("strike"))
    if usable_price(strike) is None:
        return None
    expiration = parse_expiration(mapping.get("drillDownURL")) or fallback_expiration
    if expiration is None:
        return None
    root, identity_reason = row_identity(mapping.get("drillDownURL"), ticker, expiration, strike)
    if fallback_expiration is not None and fallback_expiration != expiration:
        identity_reason = "Nasdaq expiry group differs from contract symbol"
    return OptionQuote(
        ticker=ticker,
        expiration=expiration,
        strike=strike,
        root=root,
        identity_reason=identity_reason,
        call_bid=parse_price(mapping.get("c_Bid")),
        call_ask=parse_price(mapping.get("c_Ask")),
        call_volume=parse_nonnegative_int(mapping.get("c_Volume")),
        call_open_interest=parse_nonnegative_int(mapping.get("c_Openinterest")),
        put_bid=parse_price(mapping.get("p_Bid")),
        put_ask=parse_price(mapping.get("p_Ask")),
        put_volume=parse_nonnegative_int(mapping.get("p_Volume")),
        put_open_interest=parse_nonnegative_int(mapping.get("p_Openinterest")),
    )


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_stock_info(ticker: Ticker, payload: Any, fetched_at: datetime) -> StockInfoResponse:
    root = _as_mapping(payload)
    data = _as_mapping(root.get("data")) if root else None
    primary = _as_mapping(data.get("primaryData")) if data else None
    if primary is None:
        raise ValueError("Nasdaq response is missing data.primaryData")
    raw_realtime = primary.get("isRealTime")
    is_real_time = raw_realtime is True or (
        isinstance(raw_realtime, str) and raw_realtime.strip().lower() == "true"
    )
    return StockInfoResponse(
        ticker=ticker,
        fetched_at=fetched_at,
        from_cache=False,
        bid=parse_price(primary.get("bidPrice")),
        ask=parse_price(primary.get("askPrice")),
        quote_timestamp=_clean_text(primary.get("lastTradeTimestamp")),
        is_real_time=is_real_time,
        market_session=_clean_text(data.get("marketStatus")) if data else None,
    )


def _parse_historical_date(value: Any) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_historical_bars(payload: Any) -> list[HistoricalBar]:
    root = _as_mapping(payload)
    data = _as_mapping(root.get("data")) if root else None
    if data is None:
        raise ValueError("Nasdaq response is missing data")
    table = _as_mapping(data.get("tradesTable")) or _as_mapping(data.get("table"))
    rows = table.get("rows") if table else None
    if not isinstance(rows, list):
        raise ValueError("Nasdaq response is missing historical rows")
    by_date: dict[str, HistoricalBar] = {}
    for row in rows:
        mapping = _as_mapping(row)
        if mapping is None:
            continue
        day = _parse_historical_date(mapping.get("date"))
        close = parse_price(str(mapping.get("close", "")).replace("$", ""))
        if day is None or close is None or close <= 0:
            continue
        if day in by_date:
            raise ValueError(f"duplicate historical date: {day}")
        by_date[day] = HistoricalBar(
            date=date.fromisoformat(day),
            open=parse_price(str(mapping.get("open", "")).replace("$", "")),
            high=parse_price(str(mapping.get("high", "")).replace("$", "")),
            low=parse_price(str(mapping.get("low", "")).replace("$", "")),
            close=close,
            volume=parse_nonnegative_int(mapping.get("volume")),
        )
    return [by_date[day] for day in sorted(by_date)]


def parse_screener_listings(payload: Any) -> list[TickerListing]:
    root = _as_mapping(payload)
    if root is None or not nasdaq_status_ok(payload):
        raise ValueError("Nasdaq screener response is malformed")
    data = _as_mapping(root.get("data"))
    rows = data.get("rows") if data else None
    if not isinstance(rows, list):
        raise ValueError("Nasdaq screener response is missing rows")
    listings: list[TickerListing] = []
    seen: set[str] = set()
    for row in rows:
        mapping = _as_mapping(row)
        if mapping is None:
            continue
        symbol = normalize_ticker(str(mapping.get("symbol") or ""))
        if symbol is None or symbol in seen:
            continue
        name = _clean_text(mapping.get("name"))
        if name is None:
            continue
        seen.add(symbol)
        listings.append(
            TickerListing(
                symbol=symbol,
                name=name,
                sector=_clean_text(mapping.get("sector")),
                industry=_clean_text(mapping.get("industry")),
            )
        )
    return listings
