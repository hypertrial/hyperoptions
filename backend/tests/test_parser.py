from datetime import UTC, datetime
from decimal import Decimal

import pytest

from options_api.models import OptionQuote, StockInfoResponse
from options_api.parser import (
    extract_last_trade_price,
    extract_last_trade_timestamp,
    extract_table_rows,
    nasdaq_status_ok,
    parse_expiration,
    parse_expiry_group,
    parse_option_chain,
    parse_historical_bars,
    parse_price,
    parse_nonnegative_int,
    parse_stock_info,
)

from .conftest import load_fixture


def test_parse_price_nulls() -> None:
    assert parse_price(None) is None
    assert parse_price("--") is None
    assert parse_price("") is None
    assert parse_price("  ") is None
    assert parse_price("not-a-number") is None
    assert parse_price(float("nan")) is None
    assert parse_price(float("inf")) is None
    assert parse_price(True) is None


def test_parse_price_numbers() -> None:
    assert parse_price("0.50") == Decimal("0.50")
    assert parse_price("6.55") == Decimal("6.55")
    assert parse_price(4) == Decimal("4")
    assert parse_price("1,234.50") == Decimal("1234.50")
    assert parse_price("$1,234.50") == Decimal("1234.50")


def test_parse_nonnegative_integer_liquidity() -> None:
    assert parse_nonnegative_int("1,234") == 1234
    assert parse_nonnegative_int(0) == 0
    assert parse_nonnegative_int(-1) is None
    assert parse_nonnegative_int("2.5") is None
    assert parse_nonnegative_int("--") is None


def test_parse_expiration_from_drilldown_url() -> None:
    url = "/market-activity/stocks/iren/option-chain/call-put-options/iren--260918c00050000"
    assert parse_expiration(url) == "2026-09-18"
    assert parse_expiration(None) is None
    assert parse_expiration("no-date-here") is None
    assert parse_expiration("iren--991332c00050000") is None


def test_parse_expiry_group() -> None:
    assert parse_expiry_group("September 11, 2026") == "2026-09-11"
    assert parse_expiry_group("Sep 18, 2026") == "2026-09-18"
    assert parse_expiry_group("") is None
    assert parse_expiry_group(None) is None


def test_extract_last_trade_price_from_sample_payload() -> None:
    payload = load_fixture("nasdaq_iren_sample.json")
    _, _, last_trade, available = parse_option_chain("IREN", payload)
    assert available is True
    assert extract_last_trade_price(last_trade) == Decimal("43.93")
    assert extract_last_trade_timestamp(last_trade) == "SEP 10, 2026 3:37 PM ET"


def test_extract_last_trade_timestamp_rejects_missing_or_malformed_text() -> None:
    assert extract_last_trade_timestamp("LAST TRADE: $43.93") is None
    assert extract_last_trade_timestamp(None) is None


def test_parse_option_chain_skips_group_rows_and_nulls_invalid_prices() -> None:
    payload = load_fixture("nasdaq_iren_sample.json")
    rows, truncated, last_trade, available = parse_option_chain("IREN", payload)
    assert available is True
    assert truncated is False
    assert last_trade == "LAST TRADE: $43.93 (AS OF SEP 10, 2026 3:37 PM ET)"
    assert [row.expiration for row in rows] == [
        "2026-09-18",
        "2026-09-18",
        "2026-09-18",
        "2026-09-11",
    ]
    first = rows[0]
    assert first.ticker == "IREN"
    assert first.strike == Decimal("50.00")
    assert first.call_bid == Decimal("0.50")
    assert first.call_ask == Decimal("0.51")
    priced_itm = rows[1]
    assert priced_itm.strike == Decimal("48.00")
    assert priced_itm.call_bid == Decimal("0.50")
    second = rows[2]
    assert second.strike == Decimal("40.50")
    assert second.call_bid is None
    assert second.call_ask is None


def test_parse_option_chain_liquidity_fields() -> None:
    payload = {
        "data": {
            "table": {
                "rows": [
                    {
                        "strike": "40",
                        "c_Bid": "10",
                        "c_Ask": "10.20",
                        "c_Volume": "12",
                        "c_Openinterest": "55",
                        "p_Bid": "0.10",
                        "p_Ask": "0.20",
                        "p_Volume": "--",
                        "p_Openinterest": "-1",
                        "drillDownURL": "/x/iren--261016c00040000",
                    }
                ]
            }
        },
        "status": {"rCode": 200},
    }
    row = parse_option_chain("IREN", payload)[0][0]
    assert row.call_volume == 12
    assert row.call_open_interest == 55
    assert row.put_bid == Decimal("0.10")
    assert row.put_ask == Decimal("0.20")
    assert row.put_volume is None
    assert row.put_open_interest is None
    assert "put_bid" in OptionQuote.model_fields
    assert "bid_size" not in StockInfoResponse.model_fields
    assert "current_volume" not in StockInfoResponse.model_fields


@pytest.mark.parametrize("strike", ["0", "-1"])
def test_parse_option_chain_skips_nonpositive_strikes(strike: str) -> None:
    payload = {
        "data": {
            "table": {
                "rows": [
                    {
                        "strike": strike,
                        "c_Bid": "1",
                        "p_Bid": "1",
                        "drillDownURL": "/x/iren--261016c00000000",
                    },
                    {
                        "strike": "40",
                        "c_Bid": "1",
                        "p_Bid": "1",
                        "drillDownURL": "/x/iren--261016c00040000",
                    },
                ]
            }
        },
        "status": {"rCode": 200},
    }

    rows, _, _, _ = parse_option_chain("IREN", payload)
    assert [row.strike for row in rows] == [Decimal("40")]


def test_parse_stock_info_and_historical_closes() -> None:
    fetched_at = datetime(2026, 9, 11, tzinfo=UTC)
    stock = parse_stock_info(
        "IREN",
        {
            "data": {
                "marketStatus": "Market",
                "primaryData": {
                    "bidPrice": "$49.90",
                    "askPrice": "$50.10",
                    "bidSize": "12",
                    "askSize": "13",
                    "lastTradeTimestamp": "Sep 11, 2026 10:00 AM ET",
                    "isRealTime": True,
                },
            },
            "status": {"rCode": 200},
        },
        fetched_at,
    )
    assert stock.bid == Decimal("49.90")
    assert stock.ask == Decimal("50.10")
    assert stock.is_real_time is True
    assert stock.market_session == "Market"
    closes = parse_historical_bars(
        {
            "data": {
                "tradesTable": {
                    "rows": [
                        {"date": "09/11/2026", "close": "$50.00"},
                        {"date": "09/10/2026", "close": "$49.50"},
                        {"date": "bad", "close": "$1"},
                    ]
                }
            }
        }
    )
    assert [(row.date.isoformat(), row.close) for row in closes] == [
        ("2026-09-10", Decimal("49.50")),
        ("2026-09-11", Decimal("50.00")),
    ]


def test_parse_stock_info_ignores_unused_provider_quote_fields() -> None:
    stock = parse_stock_info(
        "IREN",
        {
            "data": {
                "primaryData": {
                    "bidPrice": "$49.90",
                    "askPrice": "$50.10",
                    "bidSize": "12",
                    "askSize": "13",
                    "volume": "123456",
                    "isRealTime": "true",
                },
                "keyStats": {
                    "fiftyTwoWeekHighLow": {"value": "28.93 - 76.87"}
                },
            }
        },
        datetime(2026, 9, 11, tzinfo=UTC),
    )

    assert stock.bid == Decimal("49.90")
    assert stock.ask == Decimal("50.10")
    assert stock.is_real_time is True
    assert not hasattr(stock, "fifty_two_week_low")
    assert not hasattr(stock, "bid_size")
    assert not hasattr(stock, "current_volume")


def test_parse_option_chain_falls_back_to_expirygroup_when_url_missing() -> None:
    payload = {
        "data": {
            "lastTrade": None,
            "table": {
                "rows": [
                    {
                        "expirygroup": "October 16, 2026",
                        "strike": None,
                        "drillDownURL": None,
                    },
                    {
                        "expirygroup": "",
                        "strike": "45.00",
                        "c_Bid": "1.10",
                        "c_Ask": "1.20",
                        "p_Bid": "0.40",
                        "p_Ask": "0.45",
                        "drillDownURL": None,
                    },
                ]
            },
        },
        "status": {"rCode": 200},
    }
    rows, truncated, last_trade, _available = parse_option_chain("CIFR", payload)
    assert truncated is False
    assert last_trade is None
    assert len(rows) == 1
    assert rows[0].expiration == "2026-10-16"
    assert rows[0].strike == Decimal("45.00")
    assert rows[0].call_bid == Decimal("1.10")


def test_malformed_nonempty_expirygroup_clears_previous_expiration() -> None:
    payload = {
        "data": {
            "lastTrade": None,
            "table": {
                "rows": [
                    {"expirygroup": "October 9, 2026", "strike": None},
                    {"expirygroup": "", "strike": "40", "drillDownURL": None},
                    {"expirygroup": "not an expiration", "strike": None},
                    {"expirygroup": "", "strike": "41", "drillDownURL": None},
                ]
            },
        },
        "status": {"rCode": 200},
    }

    rows, _, _, _ = parse_option_chain("CIFR", payload)

    assert [(row.strike, row.expiration) for row in rows] == [
        (Decimal("40"), "2026-10-09")
    ]


def test_options_unavailable_is_empty_not_malformed() -> None:
    from options_api.parser import options_unavailable

    payload = {
        "data": {"totalRecord": 0, "table": {"rows": None}},
        "message": "Options are not available for this symbol",
        "status": {"rCode": 200},
    }
    assert options_unavailable(payload)
    rows, truncated, last_trade, available = parse_option_chain("AACG", payload)
    assert rows == []
    assert truncated is False
    assert last_trade is None
    assert available is False


def test_malformed_payload_without_rows() -> None:
    assert nasdaq_status_ok({"status": {"rCode": 500}}) is False
    assert extract_table_rows({"data": {}}) is None
    with pytest.raises(ValueError):
        parse_option_chain("IREN", {"data": {"table": {}}})


def test_duplicate_valid_historical_dates_fail_explicitly() -> None:
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {"date": "09/10/2026", "close": "$49.50", "low": "$48.00"},
                    {"date": "09/10/2026", "close": "$49.50", "low": "$48.00"},
                ]
            }
        }
    }
    with pytest.raises(ValueError, match="duplicate historical date: 2026-09-10"):
        parse_historical_bars(payload)


def test_distinct_historical_dates_remain_valid() -> None:
    bars = parse_historical_bars(
        {
            "data": {
                "tradesTable": {
                    "rows": [
                        {"date": "09/09/2026", "close": "$49.00", "low": "$47.00"},
                        {"date": "09/10/2026", "close": "$49.50", "low": "$48.00"},
                    ]
                }
            }
        }
    )
    assert [bar.date.isoformat() for bar in bars] == ["2026-09-09", "2026-09-10"]
    assert bars[0].low == Decimal("47.00")
    assert bars[1].low == Decimal("48.00")


def test_duplicate_historical_dates_fail_before_last_write_wins() -> None:
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {"date": "09/10/2026", "close": "$49.50", "low": "$48.00"},
                    {"date": "2026-09-10", "close": "$10.00", "low": "$1.00"},
                ]
            }
        }
    }
    with pytest.raises(ValueError, match="duplicate historical date: 2026-09-10"):
        parse_historical_bars(payload)


def test_invalid_historical_rows_do_not_count_as_duplicate_dates() -> None:
    bars = parse_historical_bars(
        {
            "data": {
                "tradesTable": {
                    "rows": [
                        {"date": "bad", "close": "$49.50", "low": "$48.00"},
                        {"date": "also-bad", "close": "$49.50", "low": "$48.00"},
                        {"date": "09/10/2026", "close": "$0", "low": "$48.00"},
                        {"date": "09/10/2026", "close": "$49.50", "low": "$48.00"},
                    ]
                }
            }
        }
    )
    assert [(bar.date.isoformat(), bar.low) for bar in bars] == [
        ("2026-09-10", Decimal("48.00"))
    ]


def test_parse_option_chain_truncation_flag_follows_nasdaq_row_cap() -> None:
    def payload(count: int) -> dict:
        return {
            "data": {"table": {"rows": [{"strike": None}] * count}},
            "status": {"rCode": 200},
        }

    _, under_cap, _, _ = parse_option_chain("IREN", payload(4999))
    _, at_cap, _, _ = parse_option_chain("IREN", payload(5000))
    assert under_cap is False
    assert at_cap is True
