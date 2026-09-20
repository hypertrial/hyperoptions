from decimal import Decimal

from options_api.money import parse_decimal, to_cents, to_e4, to_pct_tenths, usable_price


def test_parse_decimal_rejects_non_finite_and_bool() -> None:
    assert parse_decimal(None) is None
    assert parse_decimal(True) is None
    assert parse_decimal(False) is None
    assert parse_decimal(float("nan")) is None
    assert parse_decimal(float("inf")) is None
    assert parse_decimal("not-a-number") is None
    assert parse_decimal("--") is None


def test_parse_decimal_reads_numeric_strings_without_float() -> None:
    assert parse_decimal("0.50") == Decimal("0.50")
    assert parse_decimal("$1,234.50") == Decimal("1234.50")
    assert parse_decimal(4) == Decimal(4)
    assert parse_decimal(Decimal("49.90")) == Decimal("49.90")


def test_quantize_half_cent_and_half_tenth_away_from_zero() -> None:
    assert to_cents(Decimal("39.995")) == 4000
    assert to_cents(Decimal("39.994")) == 3999
    assert to_cents(Decimal("-156.005")) == -15601
    assert to_pct_tenths(Decimal("42.06")) == 421
    assert to_pct_tenths(Decimal("42.04")) == 420
    assert to_pct_tenths(Decimal("-156.00000000000023")) == -1560
    assert to_pct_tenths(Decimal("-156.06")) == -1561


def test_usable_price_requires_positive_finite() -> None:
    assert usable_price(Decimal("0")) is None
    assert usable_price(Decimal("-1")) is None
    assert usable_price(Decimal("49.9")) == Decimal("49.9")


def test_to_e4_half_up() -> None:
    assert to_e4(Decimal("0.63683")) == 6368
    assert to_e4(Decimal("0.63685")) == 6369
    assert to_e4(Decimal("-0.01757")) == -176
