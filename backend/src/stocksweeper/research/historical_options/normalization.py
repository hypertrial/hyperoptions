"""Contract and exchange-session normalization, independent of application storage."""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import polars as pl

from stocksweeper.forecast.calendar import SessionCalendar

EASTERN = ZoneInfo("America/New_York")
_CONTRACT = re.compile(r"O:([A-Z]{1,5})(\d{6})([CP])(\d{8})\Z")
IDENTITY_BOUNDARIES = {"CIFR": date(2021, 8, 30), "WULF": date(2021, 12, 14)}


def contract_identity(symbol: str) -> tuple[str, str, date, Decimal]:
    match = _CONTRACT.fullmatch(symbol)
    if match is None:
        raise ValueError("nonstandard or invalid encoded option contract")
    root, expiry, side, strike = match.groups()
    maturity = datetime.strptime(expiry, "%y%m%d").date()
    encoded_strike = Decimal(strike) / 1000
    if encoded_strike <= 0:
        raise ValueError("option strike must be positive")
    return root, "call" if side == "C" else "put", maturity, encoded_strike


def normalize_contract(row: dict, calendar: SessionCalendar) -> dict:
    """Retain invalid references in the audit; never qualify adjusted roots."""
    result = {
        "contract": row.get("ticker"), "ticker": row.get("underlying_ticker"),
        "side": row.get("contract_type"), "strike": row.get("strike_price"),
        "expiry": None, "expiry_session": None,
        "shares_per_contract": row.get("shares_per_contract"),
        "exercise_style": row.get("exercise_style"),
        "valid": False, "reason": None,
        "source_load_id": row.get("_dlt_load_id"), "source_row_id": row.get("_dlt_id"),
    }
    try:
        ticker, side, expiry, strike = contract_identity(row["ticker"])
        result.update(expiry=expiry, expiry_session=calendar.expiry_session(expiry))
        if (
            ticker != row.get("underlying_ticker")
            or side != row.get("contract_type")
            or expiry != date.fromisoformat(row.get("expiration_date", ""))
            or not math.isfinite(float(row.get("strike_price")))
            or strike != Decimal(str(row.get("strike_price")))
        ):
            result["reason"] = "reference_identity_conflict"
        elif row.get("shares_per_contract") != 100:
            result["reason"] = "nonstandard_contract_terms"
        elif row.get("exercise_style") != "american":
            result["reason"] = "unsupported_exercise_style"
        elif row.get("additional_underlyings") not in (None, "", "null", "[]"):
            result["reason"] = "adjusted_contract_terms"
        else:
            result["valid"] = True
    except (KeyError, TypeError, ValueError, OverflowError):
        result["reason"] = "invalid_contract_identity"
    return result


def session_buckets(calendar: SessionCalendar, start: date, end: date) -> pl.DataFrame:
    rows = []
    for session in calendar.sessions(start, end):
        opened = calendar.exchange.session_open(session).to_pydatetime().astimezone(UTC)
        closed = calendar.exchange.session_close(session).to_pydatetime().astimezone(UTC)
        local_open = opened.astimezone(EASTERN)
        bucket = local_open.replace(minute=0, second=0, microsecond=0).astimezone(UTC)
        rows.append({
            "session": session, "session_open": opened, "session_close": closed,
            "opening_bucket": bucket, "opening_bucket_end": bucket + timedelta(hours=1),
        })
    return pl.DataFrame(rows, schema={
        "session": pl.Date, "session_open": pl.Datetime("us", "UTC"),
        "session_close": pl.Datetime("us", "UTC"),
        "opening_bucket": pl.Datetime("us", "UTC"),
        "opening_bucket_end": pl.Datetime("us", "UTC"),
    })
