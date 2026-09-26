"""Conservative identity for ordinary Nasdaq option-chain rows."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

_SYMBOL = re.compile(r"^(?P<root>[a-zA-Z0-9]{1,8})--(?P<day>\d{6})[cCpP](?P<strike>\d{8})$")
_WATCH_KEY = re.compile(
    r"^w1:(?P<ticker>[A-Z]{1,5}):(?P<root>[A-Z0-9]{1,8}):"
    r"(?P<side>call|put):(?P<expiry>\d{4}-\d{2}-\d{2}):"
    r"(?P<strike>\d{1,5}\.\d{3})$"
)


def strike_exact(strike: Decimal) -> str:
    return f"{strike:.3f}"


def row_identity(
    drill_down_url: object,
    ticker: str,
    expiration: str,
    strike: Decimal,
) -> tuple[str | None, str | None]:
    """Return root and a reason when listed terms cannot be verified as ordinary."""
    if not isinstance(drill_down_url, str) or not drill_down_url:
        return None, "Nasdaq did not provide a contract symbol"
    parsed = urlsplit(drill_down_url)
    if parsed.netloc or parsed.query or parsed.fragment:
        return None, "Nasdaq contract symbol is ambiguous"
    match = _SYMBOL.fullmatch(parsed.path.rsplit("/", 1)[-1])
    if match is None:
        return None, "Nasdaq contract symbol is unrecognized"
    root = match.group("root").upper()
    day = match.group("day")
    try:
        symbol_expiry = date(2000 + int(day[:2]), int(day[2:4]), int(day[4:])).isoformat()
    except ValueError:
        return root, "Nasdaq contract expiry is invalid"
    symbol_strike = Decimal(match.group("strike")) / Decimal(1000)
    if root != ticker:
        return root, "Adjusted or nonstandard option root"
    if symbol_expiry != expiration:
        return root, "Nasdaq contract expiry differs from chain row"
    if symbol_strike != strike:
        return root, "Nasdaq contract strike differs from chain row"
    ticker_path = re.search(r"/stocks/([^/]+)/option-chain/", parsed.path, re.IGNORECASE)
    if ticker_path is not None and ticker_path.group(1).upper() != ticker:
        return root, "Nasdaq contract ticker differs from chain row"
    return root, None


def make_watch_key(ticker: str, root: str, side: str, expiry: str, strike: Decimal) -> str:
    return f"w1:{ticker}:{root}:{side}:{expiry}:{strike_exact(strike)}"


def parse_watch_key(value: str) -> tuple[str, str, str, str, Decimal] | None:
    match = _WATCH_KEY.fullmatch(value)
    if match is None:
        return None
    try:
        date.fromisoformat(match.group("expiry"))
    except ValueError:
        return None
    strike = Decimal(match.group("strike"))
    if strike <= 0:
        return None
    return (
        match.group("ticker"),
        match.group("root"),
        match.group("side"),
        match.group("expiry"),
        strike,
    )
