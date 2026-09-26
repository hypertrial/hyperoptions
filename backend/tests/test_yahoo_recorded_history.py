"""Recorded Yahoo daily history exercised without making a network request."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from options_api.outcomes import YahooCloseProvider, resolve_outcome


def test_recorded_yahoo_split_history_preserves_dated_close_and_withholds_ambiguous_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "yahoo_aapl_2020_split.json").read_text()
    )
    assert fixture["auto_adjust"] is False
    assert fixture["actions"] is True
    rows = fixture["rows"]
    full_frame = pd.DataFrame(
        {
            "Close": [float(row["close"]) for row in rows],
            "Stock Splits": [float(row["stock_splits"]) for row in rows],
        },
        index=pd.to_datetime([row["date"] for row in rows]).tz_localize("America/New_York"),
    )
    requests: list[dict[str, object]] = []

    class RecordedTicker:
        def history(self, **kwargs):
            requests.append(kwargs)
            start = date.fromisoformat(kwargs["start"])
            end = date.fromisoformat(kwargs["end"])
            return full_frame[(full_frame.index.date >= start) & (full_frame.index.date < end)]

    def ticker(symbol: str) -> RecordedTicker:
        assert symbol == "AAPL"
        return RecordedTicker()

    monkeypatch.setattr("yfinance.Ticker", ticker)
    provider = YahooCloseProvider()
    history = provider.fetch("AAPL", date(2020, 8, 27), date(2020, 9, 5))
    assert history.closes[date(2020, 8, 28)] == Decimal("124.80750274658203")
    assert history.closes[date(2020, 9, 4)] == Decimal("120.95999908447266")
    assert history.split_dates == frozenset({date(2020, 8, 31)})
    assert history.actions_verified
    assert requests[0]["auto_adjust"] is False
    assert requests[0]["actions"] is True

    after_split = resolve_outcome(
        ticker="AAPL",
        root="AAPL",
        side="call",
        strike=Decimal("121"),
        expiration=date(2020, 9, 4),
        watched_at=datetime(2020, 9, 1, 21, tzinfo=UTC),
        as_of=datetime(2020, 9, 4, 22, tzinfo=UTC),
        provider=provider,
    )
    assert after_split.status == "provisional"
    assert after_split.classification == "otm"
    assert after_split.session_date == date(2020, 9, 4)
    assert after_split.close_price == Decimal("120.95999908447266")

    crossed_split = resolve_outcome(
        ticker="AAPL",
        root="AAPL",
        side="call",
        strike=Decimal("125"),
        expiration=date(2020, 9, 4),
        watched_at=datetime(2020, 8, 27, 21, tzinfo=UTC),
        as_of=datetime(2020, 9, 4, 22, tzinfo=UTC),
        provider=provider,
    )
    assert crossed_split.status == "unsupported"
    assert crossed_split.classification is None
    assert crossed_split.close_price is None
    assert "split" in (crossed_split.reason or "").lower()
    assert crossed_split.preserve_prior is False

    later_recheck = resolve_outcome(
        ticker="AAPL",
        root="AAPL",
        side="call",
        strike=Decimal("125"),
        expiration=date(2020, 8, 28),
        watched_at=datetime(2020, 8, 27, 21, tzinfo=UTC),
        as_of=datetime(2020, 9, 4, 22, tzinfo=UTC),
        provider=provider,
    )
    assert later_recheck.status == "unsupported"
    assert later_recheck.preserve_prior is True
    assert "post-expiry stock split" in (later_recheck.reason or "").lower()
