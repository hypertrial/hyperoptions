from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
TEN = Decimal("10")
HUNDRED = Decimal("100")
DAYS_PER_YEAR = Decimal("365")
SHARES_PER_CONTRACT = 100


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            return None
        text = format(value, ".15g")
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    else:
        text = str(value).strip()
        if not text or text == "--":
            return None
        text = text.replace(",", "").removeprefix("$")
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    if not number.is_finite():
        return None
    return number


def usable_price(value: Decimal | None) -> Decimal | None:
    if value is None or not value.is_finite() or value <= ZERO:
        return None
    return value


def to_cents(value: Decimal) -> int:
    return int((value * HUNDRED).to_integral_value(rounding=ROUND_HALF_UP))


def to_pct_tenths(value: Decimal) -> int:
    return int((value * TEN).to_integral_value(rounding=ROUND_HALF_UP))


E4 = Decimal("10000")


def to_e4(value: Decimal) -> int:
    return int((value * E4).to_integral_value(rounding=ROUND_HALF_UP))


def optional_cents(value: Decimal | None) -> int | None:
    return None if value is None else to_cents(value)


def optional_pct_tenths(value: Decimal | None) -> int | None:
    return None if value is None else to_pct_tenths(value)


def optional_e4(value: Decimal | None) -> int | None:
    return None if value is None else to_e4(value)
