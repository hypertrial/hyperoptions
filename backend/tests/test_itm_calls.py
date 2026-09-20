from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from options_api.chain import assemble_cash_secured_puts, assemble_covered_calls, period_low
from options_api.models import (
    HistoricalBar,
    HistoricalResponse,
    OptionChainResponse,
    OptionQuote,
    PeriodLows,
    StockInfoResponse,
)
from options_api.money import parse_decimal, to_cents, to_pct_tenths

NOW = datetime(2026, 9, 11, 14, tzinfo=UTC)
TODAY = date(2026, 9, 11)
D = Decimal


def _d(value: float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return parse_decimal(value)


def _quote(
    expiration: str,
    strike: float | Decimal,
    bid: float | Decimal | None = 10.0,
    ask: float | Decimal | None = 10.2,
    volume: int | None = 25,
    open_interest: int | None = 80,
) -> OptionQuote:
    return OptionQuote(
        ticker="IREN",
        expiration=expiration,
        strike=_d(strike) or D(0),
        call_bid=_d(bid),
        call_ask=_d(ask),
        call_volume=volume,
        call_open_interest=open_interest,
    )


def _put_quote(
    expiration: str,
    strike: float | Decimal,
    bid: float | Decimal | None = 1.2,
    ask: float | Decimal | None = 1.3,
    volume: int | None = 25,
    open_interest: int | None = 80,
) -> OptionQuote:
    return OptionQuote(
        ticker="IREN",
        expiration=expiration,
        strike=_d(strike) or D(0),
        call_bid=None,
        call_ask=None,
        put_bid=_d(bid),
        put_ask=_d(ask),
        put_volume=volume,
        put_open_interest=open_interest,
    )


def _sided_quote(
    expiration: str,
    strike: float | Decimal,
    call_open_interest: int | None = 80,
    put_open_interest: int | None = 80,
) -> OptionQuote:
    return OptionQuote(
        ticker="IREN",
        expiration=expiration,
        strike=_d(strike) or D(0),
        call_bid=D("1.00"),
        call_ask=D("1.10"),
        call_open_interest=call_open_interest,
        put_bid=D("1.20"),
        put_ask=D("1.30"),
        put_open_interest=put_open_interest,
    )


def _chain(
    *rows: OptionQuote, truncated: bool = False, spot: float | Decimal | None = 43.93
) -> OptionChainResponse:
    return OptionChainResponse(
        ticker="IREN",
        fetched_at=NOW,
        from_cache=False,
        last_trade="LAST TRADE: $43.93 (AS OF SEP 10, 2026 3:37 PM ET)",
        last_trade_timestamp="SEP 10, 2026 3:37 PM ET",
        spot=_d(spot),
        truncated=truncated,
        rows=list(rows),
    )


def _info(
    ask: float | Decimal | None = 50.1, bid: float | Decimal | None = 49.9
) -> StockInfoResponse:
    return StockInfoResponse(
        ticker="IREN",
        fetched_at=NOW,
        from_cache=False,
        bid=_d(bid),
        ask=_d(ask),
        quote_timestamp="Sep 11, 2026 10:00 AM ET",
        is_real_time=True,
        market_session="Market",
    )


def _history(bars: list[HistoricalBar] | None = None) -> HistoricalResponse:
    return HistoricalResponse(
        ticker="IREN",
        fetched_at=NOW,
        from_cache=False,
        bars=bars
        or [
            HistoricalBar(date=date(2026, 9, 10), low=D("40.0"), close=D("49.5")),
            HistoricalBar(date=date(2026, 8, 20), low=D("35.0"), close=D("42.0")),
            HistoricalBar(date=date(2026, 6, 15), low=D("30.0"), close=D("38.0")),
            HistoricalBar(date=date(2025, 10, 1), low=D("20.0"), close=D("25.0")),
        ],
    )


def _empty_lows() -> PeriodLows:
    return PeriodLows(d7_cents=None, d30_cents=None, d90_cents=None, d365_cents=None)


def test_assemble_groups_itm_calls_and_computes_zero_cost_metrics() -> None:
    page = assemble_covered_calls(
        _chain(
            _quote("2026-10-09", 45, bid=8.0, ask=8.2),
            _quote("2026-09-18", 40.5, bid=None, ask=None),
            _quote("2026-09-18", 50.0, bid=0.5, ask=0.51),
            _quote("2026-09-18", 50.0, bid=9.0, ask=9.1),
            _quote("2026-09-18", 50.1, bid=1.0, ask=1.1),
            _quote("2026-09-11", 44.0, bid=0.88, ask=0.91),
            _quote("2026-10-09", 51.0, bid=2.0, ask=2.1),
        ),
        _info(),
        _history(),
        TODAY,
        NOW,
    )

    assert page.current_cents == 4990
    assert page.current_source == "stock_bid"
    assert [group.expiration for group in page.expirations] == [
        "2026-09-18",
        "2026-10-09",
    ]
    near = page.expirations[0]
    assert near.dte == 7
    assert [row.strike_cents for row in near.contracts] == [4050]
    assert [
        row.strike_cents for group in page.expirations for row in group.contracts
    ] == [4050, 4500]
    missing = near.contracts[0]
    assert missing.stock_cost_cents == to_cents(D("100") * D("49.9"))
    assert missing.premium_cents is None
    assert missing.outlay_cents is None
    assert missing.effective_cost_cents is None
    assert missing.called_pnl_cents is None
    assert missing.called_pnl_per_share_cents is None
    assert missing.simple_apr_pct_tenths is None
    assert missing.stock_apr_pct_tenths is None
    assert missing.drop_to_breakeven_pct_tenths is None
    assert missing.drop_to_strike_pct_tenths == to_pct_tenths(
        (D("49.9") - D("40.5")) / D("49.9") * D("100")
    )
    assert missing.vs_7d_low_pct_tenths == to_pct_tenths(
        (D("40.5") - D("40")) / D("40") * D("100")
    )
    assert missing.vs_30d_low_pct_tenths == to_pct_tenths(
        (D("40.5") - D("35")) / D("35") * D("100")
    )
    assert missing.vs_90d_low_pct_tenths == to_pct_tenths(
        (D("40.5") - D("30")) / D("30") * D("100")
    )
    assert missing.vs_365d_low_pct_tenths == to_pct_tenths(
        (D("40.5") - D("20")) / D("20") * D("100")
    )
    later = page.expirations[1].contracts[0]
    assert later.strike_cents == 4500
    assert later.dte == 28
    assert later.stock_cost_cents == to_cents(D("100") * D("49.9"))
    assert later.premium_cents == to_cents(D("100") * D("8.0"))
    assert later.outlay_cents == to_cents(D("100") * (D("49.9") - D("8.0")))
    assert later.outlay_cents == later.stock_cost_cents - later.premium_cents
    assert later.effective_cost_cents == to_cents(D("49.9") - D("8.0"))
    assert later.called_pnl_cents == to_cents(
        D("100") * D("8.0") - D("100") * (D("49.9") - D("45"))
    )
    assert later.called_pnl_per_share_cents == to_cents(D("45") + D("8.0") - D("49.9"))
    assert later.called_pnl_cents == 100 * later.called_pnl_per_share_cents
    assert later.drop_to_strike_pct_tenths == to_pct_tenths(
        (D("49.9") - D("45")) / D("49.9") * D("100")
    )
    assert later.drop_to_breakeven_pct_tenths == to_pct_tenths(
        D("8.0") / D("49.9") * D("100")
    )
    assert later.call_spread_cents == to_cents(D("0.2"))
    assert later.call_spread_pct_tenths == to_pct_tenths(D("0.2") / D("8.1") * D("100"))


def test_last_trade_fallback_and_equal_strike_exclusion() -> None:
    page = assemble_covered_calls(
        _chain(
            _quote("2026-09-18", 40.5, bid=4.0, ask=4.2),
            _quote("2026-09-18", 43.93, bid=1.0, ask=1.1),
        ),
        _info(ask=None, bid=None),
        _history(),
        TODAY,
        NOW,
    )
    assert page.current_cents == 4393
    assert page.current_source == "chain_last_trade"
    assert [row.strike_cents for row in page.expirations[0].contracts] == [4050]
    row = page.expirations[0].contracts[0]
    assert row.drop_to_strike_pct_tenths == to_pct_tenths(
        (D("43.93") - D("40.5")) / D("43.93") * D("100")
    )
    assert row.drop_to_breakeven_pct_tenths == to_pct_tenths(
        D("4.0") / D("43.93") * D("100")
    )
    assert row.outlay_cents == to_cents(D("100") * (D("43.93") - D("4.0")))


def test_missing_current_leaves_the_page_unpriced() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40.5), spot=None),
        _info(ask=None, bid=None),
        _history(),
        TODAY,
        NOW,
    )
    assert page.current_cents is None
    assert page.current_source is None
    assert page.expirations == []


def test_nonpositive_outlay_keeps_row_without_apr() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40, bid=60.0, ask=61.0)),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    row = page.expirations[0].contracts[0]
    assert row.stock_cost_cents == to_cents(D("100") * D("49.9"))
    assert row.premium_cents == to_cents(D("100") * D("60.0"))
    assert row.outlay_cents == to_cents(D("-1010"))
    assert row.effective_cost_cents == to_cents(D("49.9") - D("60.0"))
    assert row.called_pnl_cents == to_cents(D("5010"))
    assert row.called_pnl_per_share_cents == to_cents(D("40") + D("60.0") - D("49.9"))
    assert row.called_pnl_cents == 100 * row.called_pnl_per_share_cents
    assert row.simple_apr_pct_tenths is None
    assert row.stock_apr_pct_tenths == to_pct_tenths(
        D("5010") / D("4990") * D("365") / D("7") * D("100")
    )
    assert row.drop_to_breakeven_pct_tenths == to_pct_tenths(
        D("60.0") / D("49.9") * D("100")
    )
    assert row.drop_to_breakeven_pct_tenths is not None
    assert row.drop_to_breakeven_pct_tenths > 1000


def test_missing_low_window_stays_unavailable() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40)),
        _info(),
        _history([HistoricalBar(date=date(2026, 8, 20), low=D("32.0"), close=D("49.5"))]),
        TODAY,
        NOW,
    )
    row = page.expirations[0].contracts[0]
    assert row.vs_7d_low_pct_tenths is None
    assert row.vs_30d_low_pct_tenths == to_pct_tenths(
        (D("40") - D("32")) / D("32") * D("100")
    )
    assert row.vs_90d_low_pct_tenths == to_pct_tenths(
        (D("40") - D("32")) / D("32") * D("100")
    )
    assert row.vs_365d_low_pct_tenths == to_pct_tenths(
        (D("40") - D("32")) / D("32") * D("100")
    )


def test_period_low_uses_completed_calendar_window() -> None:
    bars = [
        HistoricalBar(date=date(2026, 9, 11), low=D("10.0"), close=D("50.0")),
        HistoricalBar(date=date(2026, 9, 4), low=D("12.0"), close=D("40.0")),
        HistoricalBar(date=date(2026, 9, 3), low=D("8.0"), close=D("38.0")),
    ]
    assert period_low(bars, TODAY, 7) == D("12.0")
    assert period_low(bars, TODAY, 8) == D("8.0")


def test_truncation_flag_is_preserved() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40), truncated=True),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    assert page.truncated is True


def test_zero_outlay_keeps_row_without_apr() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40, bid=49.9, ask=50.2)),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    row = page.expirations[0].contracts[0]
    assert row.stock_cost_cents == to_cents(D("100") * D("49.9"))
    assert row.premium_cents == to_cents(D("100") * D("49.9"))
    assert row.outlay_cents == 0
    assert row.called_pnl_cents == to_cents(D("4000"))
    assert row.called_pnl_per_share_cents == to_cents(D("40") + D("49.9") - D("49.9"))
    assert row.called_pnl_cents == 100 * row.called_pnl_per_share_cents
    assert row.simple_apr_pct_tenths is None
    assert row.stock_apr_pct_tenths == to_pct_tenths(
        D("4000") / D("4990") * D("365") / D("7") * D("100")
    )


def test_nonpositive_call_bid_keeps_itm_row_without_cash_metrics() -> None:
    page = assemble_covered_calls(
        _chain(
            _quote("2026-09-18", 40, bid=0.0, ask=0.1),
            _quote("2026-10-09", 41, bid=-1.0, ask=0.2),
        ),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    rows = [row for group in page.expirations for row in group.contracts]
    assert [row.strike_cents for row in rows] == [4000, 4100]
    for row in rows:
        assert row.stock_cost_cents == to_cents(D("100") * D("49.9"))
        assert row.premium_cents is None
        assert row.outlay_cents is None
        assert row.effective_cost_cents is None
        assert row.called_pnl_cents is None
        assert row.called_pnl_per_share_cents is None
        assert row.simple_apr_pct_tenths is None
        assert row.stock_apr_pct_tenths is None
        assert row.drop_to_breakeven_pct_tenths is None


def test_past_expiration_and_otm_only_groups_are_omitted() -> None:
    page = assemble_covered_calls(
        _chain(
            _quote("2026-09-10", 40, bid=5.0, ask=5.1),
            _quote("2026-09-18", 60, bid=0.4, ask=0.5),
            _quote("2026-10-09", 40, bid=4.0, ask=4.1),
            OptionQuote(
                ticker="IREN",
                expiration="not-a-date",
                strike=D("30.0"),
                call_bid=D("8.0"),
                call_ask=D("8.1"),
            ),
        ),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    assert [group.expiration for group in page.expirations] == ["2026-10-09"]
    assert [row.strike_cents for row in page.expirations[0].contracts] == [4000]


def test_nonpositive_bid_falls_back_to_last_trade_even_when_ask_is_valid() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40.5, bid=4.0, ask=4.2)),
        _info(ask=50.1, bid=0.0),
        _history(),
        TODAY,
        NOW,
    )
    assert page.current_cents == 4393
    assert page.current_source == "chain_last_trade"
    assert [row.strike_cents for row in page.expirations[0].contracts] == [4050]


def test_missing_bid_ignores_valid_ask_and_uses_last_trade() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40.5, bid=4.0, ask=4.2)),
        _info(ask=50.1, bid=None),
        _history(),
        TODAY,
        NOW,
    )
    assert page.current_cents == 4393
    assert page.current_source == "chain_last_trade"
    assert [row.strike_cents for row in page.expirations[0].contracts] == [4050]


def test_nonpositive_ask_falls_back_to_last_trade() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40.5, bid=4.0, ask=4.2)),
        _info(ask=0.0, bid=None),
        _history(),
        TODAY,
        NOW,
    )
    assert page.current_cents == 4393
    assert page.current_source == "chain_last_trade"
    assert [row.strike_cents for row in page.expirations[0].contracts] == [4050]


def test_invalid_bid_and_last_trade_leave_truncated_page_unpriced() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40.5), truncated=True, spot=0.0),
        _info(ask=50.1, bid=-8.0),
        _history(),
        TODAY,
        NOW,
    )
    assert page.current_cents is None
    assert page.current_source is None
    assert page.truncated is True
    assert page.expirations == []


def test_duplicate_expiry_strike_keeps_first_occurrence() -> None:
    page = assemble_covered_calls(
        _chain(
            _quote("2026-09-18", 40, bid=5.0, ask=5.2),
            _quote("2026-09-18", 40, bid=9.0, ask=9.2),
        ),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    contracts = page.expirations[0].contracts
    assert [row.strike_cents for row in contracts] == [4000]
    assert contracts[0].call_bid_cents == 500
    assert contracts[0].outlay_cents == to_cents(D("100") * (D("49.9") - D("5.0")))


def test_fifty_two_week_low_is_never_a_period_low_substitute() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40)),
        _info(),
        _history([HistoricalBar(date=date(2025, 9, 10), low=D("18.0"), close=D("25.0"))]),
        TODAY,
        NOW,
    )
    row = page.expirations[0].contracts[0]
    assert page.lows == _empty_lows()
    assert row.vs_7d_low_pct_tenths is None
    assert row.vs_30d_low_pct_tenths is None
    assert row.vs_90d_low_pct_tenths is None
    assert row.vs_365d_low_pct_tenths is None


def test_period_low_ignores_nonpositive_and_missing_lows() -> None:
    bars = [
        HistoricalBar(date=date(2026, 9, 10), low=None, close=D("49.0")),
        HistoricalBar(date=date(2026, 9, 9), low=D("0.0"), close=D("48.0")),
        HistoricalBar(date=date(2026, 9, 8), low=D("-3.0"), close=D("47.0")),
        HistoricalBar(date=date(2026, 9, 7), low=D("33.0"), close=D("46.0")),
    ]
    assert period_low(bars, TODAY, 7) == D("33.0")
    assert period_low(bars[:3], TODAY, 7) is None


def test_period_low_365_includes_start_and_excludes_today() -> None:
    bars = [
        HistoricalBar(date=date(2025, 9, 10), low=D("5.0"), close=D("10.0")),
        HistoricalBar(date=date(2025, 9, 11), low=D("20.0"), close=D("21.0")),
        HistoricalBar(date=date(2026, 9, 11), low=D("1.0"), close=D("50.0")),
    ]
    assert period_low(bars, TODAY, 365) == D("20.0")


def test_signed_low_is_negative_when_strike_is_below_period_low() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", 40)),
        _info(),
        _history([HistoricalBar(date=date(2026, 9, 10), low=D("45.0"), close=D("49.5"))]),
        TODAY,
        NOW,
    )
    row = page.expirations[0].contracts[0]
    assert row.vs_7d_low_pct_tenths == to_pct_tenths(
        (D("40") - D("45")) / D("45") * D("100")
    )
    assert row.vs_365d_low_pct_tenths == to_pct_tenths(
        (D("40") - D("45")) / D("45") * D("100")
    )


def test_open_interest_floor_keeps_oi_of_five_and_drops_thinner_rows() -> None:
    page = assemble_covered_calls(
        _chain(
            _quote("2026-09-18", 49, open_interest=5),
            _quote("2026-09-18", 48, open_interest=4),
            _quote("2026-09-18", 46, open_interest=0),
            _quote("2026-09-18", 44, open_interest=None),
            _quote("2026-09-18", 42, open_interest=80),
            _quote("2026-10-09", 45, open_interest=4),
        ),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    assert [group.expiration for group in page.expirations] == ["2026-09-18"]
    assert [row.strike_cents for row in page.expirations[0].contracts] == [4900, 4200]
    assert [row.call_open_interest for row in page.expirations[0].contracts] == [5, 80]


def test_high_apr_rows_remain_when_open_interest_qualifies() -> None:
    page = assemble_covered_calls(
        _chain(
            _quote("2026-09-18", 49, bid=0.5, ask=0.51, open_interest=5),
            _quote("2026-09-18", 40, bid=40.0, ask=40.2, open_interest=6),
        ),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    strikes = [row.strike_cents for row in page.expirations[0].contracts]
    aprs = [row.simple_apr_pct_tenths for row in page.expirations[0].contracts]
    assert strikes == [4900, 4000]
    shallow_outlay = D("100") * (D("49.9") - D("0.5"))
    shallow_pnl = D("100") * D("49") - shallow_outlay
    assert aprs[0] == to_pct_tenths(shallow_pnl / shallow_outlay * D("365") / D("7") * D("100"))
    assert aprs[1] is not None
    assert aprs[1] > aprs[0]


def test_effective_cost_uses_call_bid_without_requiring_ask() -> None:
    page = assemble_covered_calls(
        _chain(
            _quote("2026-09-18", 48, bid=0.5, ask=0.51),
            _quote("2026-09-18", 46, bid=1.0, ask=None),
            _quote("2026-09-18", 44, bid=None, ask=1.2),
        ),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    rows = {row.strike_cents: row for row in page.expirations[0].contracts}
    assert rows[4800].effective_cost_cents == to_cents(D("49.9") - D("0.5"))
    assert rows[4600].effective_cost_cents == to_cents(D("49.9") - D("1.0"))
    assert rows[4600].outlay_cents == to_cents(D("100") * (D("49.9") - D("1.0")))
    assert rows[4400].effective_cost_cents is None
    assert rows[4400].outlay_cents is None
    assert rows[4400].stock_cost_cents == to_cents(D("100") * D("49.9"))
    assert rows[4400].premium_cents is None
    assert rows[4400].stock_apr_pct_tenths is None


def test_effective_cost_null_for_nonpositive_or_nonfinite_call_bid() -> None:
    page = assemble_covered_calls(
        _chain(
            _quote("2026-09-18", 48, bid=1.0, ask=0.0),
            _quote("2026-09-18", 46, bid=1.0, ask=-0.01),
            _quote("2026-09-18", 44, bid=1.0, ask=float("inf")),
            _quote("2026-09-18", 42, bid=1.0, ask=float("nan")),
            _quote("2026-09-18", 40, bid=float("nan"), ask=1.2),
            _quote("2026-09-18", 38, bid=float("inf"), ask=1.2),
        ),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    rows = {row.strike_cents: row for row in page.expirations[0].contracts}
    expected = to_cents(D("49.9") - D("1.0"))
    expected_outlay = to_cents(D("100") * (D("49.9") - D("1.0")))
    assert rows[4800].effective_cost_cents == expected
    assert rows[4600].effective_cost_cents == expected
    assert rows[4400].effective_cost_cents == expected
    assert rows[4200].effective_cost_cents == expected
    assert rows[4000].effective_cost_cents is None
    assert rows[3800].effective_cost_cents is None
    assert rows[4800].outlay_cents == expected_outlay
    assert rows[4600].outlay_cents == expected_outlay
    assert rows[4400].outlay_cents == expected_outlay
    assert rows[4200].outlay_cents == expected_outlay
    assert rows[4000].outlay_cents is None
    assert rows[3800].outlay_cents is None


def test_assemble_quantizes_half_cent_and_half_tenth_with_round_half_up() -> None:
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", D("39.995"), bid=D("8.005"), ask=D("8.015"))),
        _info(ask=D("50.1"), bid=D("49.995")),
        _history(),
        TODAY,
        NOW,
    )
    assert page.current_cents == 5000
    row = page.expirations[0].contracts[0]
    assert row.strike_cents == 4000
    assert row.call_bid_cents == 801
    assert row.call_spread_cents == 1
    assert row.called_pnl_per_share_cents == to_cents(
        D("39.995") + D("8.005") - D("49.995")
    )
    assert row.called_pnl_cents == to_cents(
        D("100") * D("39.995") - D("100") * (D("49.995") - D("8.005"))
    )
    assert row.call_bid_cents is not None
    assert page.current_cents is not None
    assert row.called_pnl_per_share_cents != (
        row.strike_cents + row.call_bid_cents - page.current_cents
    )


def test_called_pnl_per_share_quantizes_independently_on_sub_cent_inputs() -> None:
    strike = D("40.004")
    bid = D("8.004")
    current = D("48.003")
    page = assemble_covered_calls(
        _chain(_quote("2026-09-18", strike, bid=bid, ask=D("8.014"))),
        _info(ask=D("48.1"), bid=current),
        _history(),
        TODAY,
        NOW,
    )
    row = page.expirations[0].contracts[0]
    per_share = to_cents(strike + bid - current)
    called = to_cents(D("100") * strike - D("100") * (current - bid))
    assert row.called_pnl_per_share_cents == per_share
    assert row.called_pnl_cents == called
    assert called != 100 * per_share
    assert row.called_pnl_per_share_cents != called // 100
    assert row.call_bid_cents is not None
    assert page.current_cents is not None
    assert row.called_pnl_per_share_cents != (
        row.strike_cents + row.call_bid_cents - page.current_cents
    )


def test_assemble_cash_secured_puts_computes_csp_metrics_on_strike() -> None:
    page = assemble_cash_secured_puts(
        _chain(_put_quote("2026-09-18", 45, bid=1.20, ask=1.30)),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    assert page.moneyness == "otm"
    assert page.current_cents == 4990
    assert [row.strike_cents for row in page.expirations[0].contracts] == [4500]
    row = page.expirations[0].contracts[0]
    assert not hasattr(row, "assigned_pnl")
    assert "assigned_pnl" not in type(row).model_fields
    assert row.premium_cents == to_cents(D("100") * D("1.20"))
    assert row.collateral_cents == to_cents(D("100") * D("45"))
    assert row.net_collateral_cents == row.collateral_cents - row.premium_cents
    assert row.breakeven_cents == to_cents(D("45") - D("1.20"))
    assert row.apr_collateral_pct_tenths == to_pct_tenths(
        D("120") / D("4500") * D("365") / D("7") * D("100")
    )
    assert row.apr_net_pct_tenths == to_pct_tenths(
        D("120") / D("4380") * D("365") / D("7") * D("100")
    )
    assert row.cushion_to_strike_pct_tenths == to_pct_tenths(
        (D("49.9") - D("45")) / D("49.9") * D("100")
    )
    assert row.cushion_to_breakeven_pct_tenths == to_pct_tenths(
        (D("49.9") - D("43.80")) / D("49.9") * D("100")
    )
    assert row.put_spread_cents == to_cents(D("0.10"))
    assert row.put_spread_pct_tenths == to_pct_tenths(D("0.10") / D("1.25") * D("100"))
    assert row.vs_7d_low_pct_tenths == to_pct_tenths(
        (D("45") - D("40")) / D("40") * D("100")
    )
    assert row.vs_30d_low_pct_tenths == to_pct_tenths(
        (D("45") - D("35")) / D("35") * D("100")
    )
    assert row.vs_90d_low_pct_tenths == to_pct_tenths(
        (D("45") - D("30")) / D("30") * D("100")
    )
    assert row.vs_365d_low_pct_tenths == to_pct_tenths(
        (D("45") - D("20")) / D("20") * D("100")
    )


def test_put_open_interest_floor_and_zero_dte_are_side_specific() -> None:
    page = assemble_cash_secured_puts(
        _chain(
            _put_quote("2026-09-18", 45, open_interest=5),
            _put_quote("2026-09-18", 44, open_interest=4),
            _put_quote("2026-09-18", 43, open_interest=None),
            _put_quote("2026-09-11", 42, open_interest=80),
            _quote("2026-09-18", 40, open_interest=80),
        ),
        _info(),
        _history(),
        TODAY,
        NOW,
    )
    assert [row.strike_cents for row in page.expirations[0].contracts] == [4500]
    assert page.expirations[0].contracts[0].put_open_interest == 5


def test_atm_is_excluded_from_itm_and_included_in_otm_and_all() -> None:
    rows = (
        _sided_quote("2026-09-18", 50.0),
        _sided_quote("2026-09-18", 49.9),
        _sided_quote("2026-09-18", 45.0),
    )
    call_itm = assemble_covered_calls(_chain(*rows), _info(), _history(), TODAY, NOW, "itm")
    call_otm = assemble_covered_calls(_chain(*rows), _info(), _history(), TODAY, NOW, "otm")
    call_all = assemble_covered_calls(_chain(*rows), _info(), _history(), TODAY, NOW, "all")
    put_itm = assemble_cash_secured_puts(_chain(*rows), _info(), _history(), TODAY, NOW, "itm")
    put_otm = assemble_cash_secured_puts(_chain(*rows), _info(), _history(), TODAY, NOW, "otm")
    put_all = assemble_cash_secured_puts(_chain(*rows), _info(), _history(), TODAY, NOW, "all")

    assert [row.strike_cents for row in call_itm.expirations[0].contracts] == [4500]
    assert [row.strike_cents for row in call_otm.expirations[0].contracts] == [5000, 4990]
    assert [row.strike_cents for row in call_all.expirations[0].contracts] == [5000, 4990, 4500]
    assert [row.strike_cents for row in put_itm.expirations[0].contracts] == [5000]
    assert [row.strike_cents for row in put_otm.expirations[0].contracts] == [4990, 4500]
    assert [row.strike_cents for row in put_all.expirations[0].contracts] == [5000, 4990, 4500]
    assert call_itm.expirations[0].contracts[0].in_the_money is True
    assert call_otm.expirations[0].contracts[0].in_the_money is False
    assert put_itm.expirations[0].contracts[0].in_the_money is True
    assert put_otm.expirations[0].contracts[0].in_the_money is False


def test_buy_write_legs_match_cifr_shaped_quote() -> None:
    trade_day = date(2026, 9, 18)
    page = assemble_covered_calls(
        _chain(
            _quote("2026-11-20", 15, bid=4.40, ask=4.50),
            spot=17.92,
        ),
        _info(ask=17.93, bid=17.92),
        _history(),
        trade_day,
        datetime(2026, 9, 18, 14, tzinfo=UTC),
    )
    row = page.expirations[0].contracts[0]
    assert page.current_cents == 1792
    assert row.dte == 63
    assert row.strike_cents == 1500
    assert row.stock_cost_cents == 179_200
    assert row.premium_cents == 44_000
    assert row.outlay_cents == 135_200
    assert row.called_pnl_cents == 14_800
    assert row.called_pnl_per_share_cents == 148
    assert row.called_pnl_cents == row.premium_cents - (
        page.current_cents - row.strike_cents
    ) * 100
    assert row.simple_apr_pct_tenths == to_pct_tenths(
        D("148") / D("1352") * D("365") / D("63") * D("100")
    )
    assert row.stock_apr_pct_tenths == to_pct_tenths(
        D("148") / D("1792") * D("365") / D("63") * D("100")
    )
    assert row.simple_apr_pct_tenths == 634
    assert row.stock_apr_pct_tenths == 478
    assert row.drop_to_breakeven_pct_tenths == to_pct_tenths(
        D("4.40") / D("17.92") * D("100")
    )
    assert row.drop_to_breakeven_pct_tenths == 246
