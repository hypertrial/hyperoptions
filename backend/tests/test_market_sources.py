from __future__ import annotations

import time
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from options_api.market_sources import (
    DividendStatus,
    TreasuryCurve,
    _yahoo_chain_sync,
    parse_treasury_curve,
)


NOW = datetime(2026, 9, 11, 14, tzinfo=UTC)


def test_treasury_uses_latest_dated_curve_and_rejects_stale_or_malformed_data() -> None:
    xml = b"""<feed xmlns:d='http://schemas.microsoft.com/ado/2007/08/dataservices'>
      <entry><content><d:NEW_DATE>2026-09-10T00:00:00</d:NEW_DATE>
        <d:BC_1MONTH>4.00</d:BC_1MONTH><d:BC_1YEAR>4.50</d:BC_1YEAR>
        <d:BC_2YEAR>5.00</d:BC_2YEAR></content></entry>
      <entry><content><d:NEW_DATE>2026-09-11T00:00:00</d:NEW_DATE>
        <d:BC_1MONTH>3.00</d:BC_1MONTH><d:BC_1YEAR>4.00</d:BC_1YEAR>
        <d:BC_2YEAR>5.00</d:BC_2YEAR></content></entry>
    </feed>"""
    curve = parse_treasury_curve(xml, NOW)
    assert curve is not None
    assert curve.as_of == date(2026, 9, 11)
    assert 0.02 < curve.rate_for(date(2026, 10, 16), NOW) < 0.04
    assert curve.rate_for(date(2026, 9, 11), NOW) is None
    assert curve.rate_for(date(2060, 1, 1), NOW) is None
    assert curve.rate_for(date(2026, 10, 16), datetime(2026, 9, 19, 14, tzinfo=UTC)) is None
    assert parse_treasury_curve(b"<entry>", NOW) is None
    assert parse_treasury_curve(b"x" * 2_000_001, NOW) is None


def test_dividend_gate_covers_ex_date_boundary_and_unknown_status() -> None:
    payer = DividendStatus("payer", NOW, date(2026, 10, 16))
    assert payer.eligible_for(date(2026, 10, 15)) == (True, None)
    assert payer.eligible_for(date(2026, 10, 16))[0] is False
    assert DividendStatus("payer", NOW).eligible_for(date(2026, 10, 15))[0] is False
    assert DividendStatus("unknown", NOW).eligible_for(date(2026, 10, 15))[0] is False
    assert DividendStatus("nonpayer", NOW).eligible_for(date(2026, 10, 16)) == (True, None)


def _frame(symbol: str, side: str, strike: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame([{
        "contractSymbol": symbol,
        "contractSize": "REGULAR",
        "currency": "USD",
        "strike": strike,
        "bid": 3.0 if side == "call" else 2.0,
        "ask": 3.1 if side == "call" else 2.1,
        "volume": 10,
        "openInterest": 50,
    }])


def test_yahoo_replacement_requires_every_reported_expiry_and_preserves_one_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expiries = ("2026-10-16", "2026-11-20")

    class Provider:
        options = expiries

        def option_chain(self, expiry: str) -> SimpleNamespace:
            code = "261016" if expiry == expiries[0] else "261120"
            return SimpleNamespace(
                calls=_frame(f"TEST{code}C00100000", "call"),
                puts=_frame(f"TEST{code}P00100000", "put"),
                underlying={
                    "regularMarketPrice": 100.0,
                    "regularMarketTime": int(NOW.timestamp()),
                },
            )

    monkeypatch.setattr("yfinance.Ticker", lambda ticker: Provider())
    chain = _yahoo_chain_sync("TEST", frozenset(expiries), time.monotonic() + 5)
    assert chain.source == "yahoo"
    assert chain.truncated is False
    assert {row.expiration for row in chain.rows} == set(expiries)
    assert len(chain.rows) == 2
    assert all(
        row.call_bid == Decimal("3.0") and row.put_bid == Decimal("2.0")
        for row in chain.rows
    )
    assert chain.spot == Decimal("100.0")

    with pytest.raises(ValueError, match="omits a Nasdaq date"):
        _yahoo_chain_sync("TEST", frozenset({*expiries, "2026-12-18"}), time.monotonic() + 5)


def test_yahoo_replacement_fails_closed_on_missing_side_or_conflicting_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        options = ("2026-10-16",)

        def option_chain(self, expiry: str) -> SimpleNamespace:
            return SimpleNamespace(
                calls=_frame("TEST261016C00100000", "call"),
                puts=None,
                underlying={},
            )

    provider = Provider()
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: provider)
    with pytest.raises(ValueError, match="side is missing"):
        _yahoo_chain_sync("TEST", frozenset(), time.monotonic() + 5)
    provider.option_chain = lambda expiry: SimpleNamespace(
        calls=_frame("TEST261016C00101000", "call"),
        puts=_frame("TEST261016P00100000", "put"),
        underlying={},
    )
    with pytest.raises(ValueError, match="terms conflict"):
        _yahoo_chain_sync("TEST", frozenset(), time.monotonic() + 5)


def test_treasury_curve_rejects_missing_points_and_future_dating() -> None:
    sparse = TreasuryCurve(date(2026, 9, 11), ((1 / 12, 0.04), (1, 0.04)))
    assert sparse.rate_for(date(2026, 10, 16), NOW) is None
    future = TreasuryCurve(date(2026, 9, 12), ((1 / 12, 0.04), (1, 0.04), (2, 0.04)))
    assert future.rate_for(date(2026, 10, 16), NOW) is None
