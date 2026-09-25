"""Deterministic option chain used by golden and memo tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from options_api.greeks import black_scholes_price
from options_api.models import (
    HistoricalBar,
    HistoricalResponse,
    OptionChainResponse,
    OptionQuote,
    StockInfoResponse,
)

NOW = datetime(2026, 9, 11, 14, tzinfo=UTC)
TODAY = date(2026, 9, 11)
RATE = Decimal("0.04")
SPOT = Decimal("50")


def _price(value: float) -> Decimal:
    return Decimal(str(round(max(value, 0.0), 2)))


def synthetic_context(
    rows_per_expiration: int = 12,
) -> tuple[OptionChainResponse, StockInfoResponse, HistoricalResponse, date, datetime]:
    quotes: list[OptionQuote] = []
    for offset in (14, 45):
        expiration = (TODAY + timedelta(days=offset)).isoformat()
        years = offset / 365
        for index in range(rows_per_expiration):
            strike = Decimal(40 + index * 2)
            sigma = 0.8
            call = black_scholes_price(True, float(SPOT), float(strike), years, 0.04, sigma)
            put = black_scholes_price(False, float(SPOT), float(strike), years, 0.04, sigma)
            call_bid = _price(call - 0.05)
            put_bid = _price(put - 0.05)
            quotes.append(
                OptionQuote(
                    ticker="IREN",
                    expiration=expiration,
                    strike=strike,
                    call_bid=call_bid if call_bid > 0 else None,
                    call_ask=_price(float(call_bid) + 0.10) if call_bid > 0 else None,
                    call_volume=index + 1,
                    call_open_interest=20 + index,
                    put_bid=put_bid if put_bid > 0 else None,
                    put_ask=_price(float(put_bid) + 0.10) if put_bid > 0 else None,
                    put_volume=index + 2,
                    put_open_interest=30 + index,
                )
            )
    chain = OptionChainResponse(
        ticker="IREN",
        fetched_at=NOW,
        from_cache=False,
        last_trade="LAST TRADE: $50.00 (AS OF SEP 11, 2026)",
        last_trade_timestamp="SEP 11, 2026",
        spot=SPOT,
        truncated=False,
        options_available=True,
        rows=quotes,
    )
    info = StockInfoResponse(
        ticker="IREN",
        fetched_at=NOW,
        from_cache=False,
        bid=Decimal("49.90"),
        ask=Decimal("50.10"),
        quote_timestamp="Sep 11, 2026 10:00 AM ET",
        is_real_time=True,
        market_session="Market",
    )
    bars = [
        HistoricalBar(
            date=TODAY - timedelta(days=day),
            low=Decimal("40.00") + Decimal(day % 5),
            close=Decimal("45.00"),
        )
        for day in range(1, 370)
    ]
    history = HistoricalResponse(
        ticker="IREN", fetched_at=NOW, from_cache=False, bars=bars
    )
    return chain, info, history, TODAY, NOW
