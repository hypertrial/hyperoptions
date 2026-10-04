import gzip
import io
import os
import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests

import research.massive_options_eod as loader


EASTERN = ZoneInfo("America/New_York")


def millis(day: date, hour: int = 12) -> int:
    moment = datetime(day.year, day.month, day.day, hour, tzinfo=EASTERN)
    return int(moment.timestamp() * 1000)


def daily(day: date, volume: float, trades: int = 10) -> dict:
    return {"t": millis(day, 0), "v": volume, "n": trades, "o": 1, "h": 1, "l": 1, "c": 1}


def hourly(day: date, hour: int, volume: float) -> dict:
    return {"t": millis(day, hour), "v": volume, "o": 1, "h": 1, "l": 1, "c": 1}


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(self._payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class ScriptedSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        return None


class PlanTests(unittest.TestCase):
    def test_budget_splits_before_the_limit(self):
        days = [daily(date(2024, 1, 1), 1, 960) for _ in range(50)]
        for offset, raw in enumerate(days):
            raw["t"] = offset
        windows = loader.plan_windows(days)
        self.assertEqual([len(window) for window in windows], [46, 4])
        self.assertLessEqual(sum(loader.minute_cost(raw) for raw in windows[0]), loader.MINUTE_BUDGET)

    def test_missing_trade_count_uses_a_full_session(self):
        raw = daily(date(2024, 1, 1), 1)
        del raw["n"]
        self.assertEqual(loader.minute_cost(raw), loader.SESSION_MINUTES)


class WindowTests(unittest.TestCase):
    def contract(self):
        return {
            "ticker": "O:TEST",
            "underlying_ticker": "TEST",
            "expiration_date": "2024-10-18",
            "strike_price": 1,
            "contract_type": "call",
        }

    def test_volume_difference_is_one_request(self):
        day = date(2024, 10, 4)
        later = date(2024, 10, 7)
        calls = []

        class Client:
            def get(self, url, params=None):
                calls.append(url)
                return {
                    "status": "OK",
                    "adjusted": True,
                    "results": [hourly(day, 10, 3), hourly(later, 11, 4)],
                }

        stats = {"hour_requests": 0, "volume_mismatch_days": 0}
        rows = loader.fetch_window(Client(), self.contract(), [daily(day, 5), daily(later, 4)], stats)
        self.assertEqual(len(calls), 1)
        self.assertEqual(stats["hour_requests"], 1)
        self.assertEqual(stats["volume_mismatch_days"], 1)
        self.assertEqual(len(rows), 2)

    def test_empty_window_splits_until_each_day(self):
        day = date(2024, 10, 4)
        later = date(2024, 10, 7)
        calls = []

        class Client:
            def get(self, url, params=None):
                calls.append(url)
                return {"status": "OK", "adjusted": True, "results": []}

        stats = {"hour_requests": 0, "volume_mismatch_days": 0}
        rows = loader.fetch_window(Client(), self.contract(), [daily(day, 5), daily(later, 4)], stats)
        self.assertEqual(rows, [])
        self.assertEqual(stats["hour_requests"], 3)
        self.assertEqual(stats["volume_mismatch_days"], 2)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.key = "secret-key-value"
        os.environ["MASSIVE_API_KEY"] = self.key

    def client(self, sessions):
        factory_sessions = list(sessions)

        def factory():
            return factory_sessions.pop(0)

        return loader.MassiveClient(self.key, session_factory=factory)

    def test_other_4xx_does_not_retry(self):
        session = ScriptedSession([FakeResponse(403, text="nope"), FakeResponse(200, {"status": "OK"})])
        client = self.client([session])
        with self.assertRaises(RuntimeError):
            client.get("https://api.massive.com/v2/aggs")
        self.assertEqual(len(session.responses), 1)

    def test_429_then_success(self):
        session = ScriptedSession(
            [FakeResponse(429, text="slow"), FakeResponse(200, {"status": "OK", "results": [1]})]
        )
        client = self.client([session])
        with patch("research.massive_options_eod.time.sleep"):
            payload = client.get("https://api.massive.com/v2/aggs")
        self.assertEqual(payload["results"], [1])

    def test_5xx_retries(self):
        session = ScriptedSession(
            [FakeResponse(503, text="down"), FakeResponse(200, {"status": "OK", "results": []})]
        )
        client = self.client([session])
        with patch("research.massive_options_eod.time.sleep"):
            payload = client.get("https://api.massive.com/v2/aggs")
        self.assertEqual(payload["status"], "OK")

    def test_connection_error_replaces_session_and_redacts_the_key(self):
        first = ScriptedSession([requests.ConnectionError(f"https://api.massive.com/?apiKey={self.key}")])
        second = ScriptedSession([FakeResponse(200, {"status": "OK", "results": []})])
        client = self.client([first, second])
        with patch("research.massive_options_eod.time.sleep"), patch(
            "research.massive_options_eod.sys.stderr"
        ) as err:
            payload = client.get("https://api.massive.com/v2/aggs")
            printed = "".join(str(call.args[0]) for call in err.write.call_args_list if call.args)
        self.assertEqual(payload["status"], "OK")
        self.assertNotIn(self.key, printed)
        self.assertIn("REDACTED", printed)


class DownloadTests(unittest.TestCase):
    def contract(self, ticker):
        return {
            "ticker": ticker,
            "underlying_ticker": "TEST",
            "expiration_date": "2024-10-18",
            "strike_price": 1,
            "contract_type": "call",
        }

    def fetch(self, ticker, fail=False):
        if fail:
            raise RuntimeError("boom")
        row = {
            "option_ticker": ticker,
            "underlying_ticker": "TEST",
            "expiration_date": "2024-10-18",
            "strike_price": 1,
            "contract_type": "call",
            "t": 1,
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "volume": 1,
            "vwap": None,
            "transactions": 1,
            "adjusted": True,
        }
        hour = dict(row, bar_start="2024-10-04T15:00:00-04:00")
        daily = dict(row, bar_date="2024-10-04")
        fetch = {
            "option_ticker": ticker,
            "results_count": 1,
            "from_date": "2024-10-04",
            "to_date": "2024-10-04",
            "fetched_at": "now",
            "daily_days": 1,
            "hour_requests": 1,
            "volume_mismatch_days": 0,
        }
        daily_fetch = {
            "option_ticker": ticker,
            "results_count": 1,
            "from_date": "2024-10-04",
            "to_date": "2024-10-04",
            "fetched_at": "now",
            "source": "rest",
        }
        return [hour], fetch, [daily], daily_fetch

    def test_failed_contract_is_skipped_and_bars_are_written_first(self):
        calls = []

        def fake_load(_pipe, specs):
            calls.append(tuple(name for name, _key, _rows in specs))

        def fake_fetch(_client, contract, *_args):
            return self.fetch(contract["ticker"], fail=contract["ticker"] == "BAD")

        pending = [self.contract("BAD"), self.contract("GOOD")]
        with patch("research.massive_options_eod.load_rows", fake_load), patch(
            "research.massive_options_eod.fetch_contract", fake_fetch
        ):
            failed = loader.download_hours(
                loader.MassiveClient("k"), object(), pending, date(2024, 10, 4), date(2024, 10, 4), 1
            )
        self.assertEqual(failed, 1)
        self.assertEqual(
            calls,
            [
                ("option_hour_bars", "option_bars"),
                ("option_hour_fetches", "option_bar_fetches"),
            ],
        )

    def test_consecutive_failures_stop_the_run(self):
        seen = []

        def fake_fetch(_client, contract, *_args):
            seen.append(contract["ticker"])
            raise RuntimeError("down")

        pending = [self.contract(f"T{index}") for index in range(6)]
        with patch("research.massive_options_eod.load_rows"), patch(
            "research.massive_options_eod.fetch_contract", fake_fetch
        ):
            failed = loader.download_hours(
                loader.MassiveClient("k"), object(), pending, date(2024, 10, 4), date(2024, 10, 4), 1
            )
        self.assertEqual(failed, 3)
        self.assertLess(len(seen), len(pending))


class FlatFileTests(unittest.TestCase):
    def test_parse_filters_tickers_and_normalizes_midnight(self):
        noon = datetime(2024, 10, 4, 12, tzinfo=EASTERN)
        nanos = int(noon.timestamp() * 1_000_000_000)
        text = (
            "ticker,volume,open,close,high,low,window_start,transactions\n"
            f"O:KEEP,5,1.5,1.6,1.7,1.4,{nanos},3\n"
            f"O:DROP,1,1,1,1,1,{nanos},1\n"
        )
        payload = gzip.compress(text.encode())
        rows = loader.parse_day_aggregates(payload, {"O:KEEP"})
        self.assertEqual(set(rows), {"O:KEEP"})
        midnight = datetime(2024, 10, 4, 0, tzinfo=EASTERN)
        self.assertEqual(rows["O:KEEP"]["t"], int(midnight.timestamp() * 1000))
        self.assertEqual(rows["O:KEEP"]["v"], 5.0)
        self.assertEqual(rows["O:KEEP"]["n"], 3)
        self.assertIsNone(rows["O:KEEP"]["vw"])

    def test_gap_after_flat_file_is_requested_from_rest(self):
        day = date(2024, 10, 4)
        gap_day = date(2024, 10, 7)
        calls = []

        class Client:
            api_key = "k"

            def get(self, url, params=None):
                calls.append(url)
                if "/range/1/day/" in url:
                    return {"status": "OK", "adjusted": True, "results": [daily(gap_day, 2, 4)]}
                return {"status": "OK", "adjusted": True, "results": [hourly(day, 10, 5), hourly(gap_day, 11, 2)]}

        contract = {
            "ticker": "O:KEEP",
            "underlying_ticker": "TEST",
            "expiration_date": "2024-10-18",
            "strike_price": 1,
            "contract_type": "call",
        }
        lookup = {"O:KEEP": [daily(day, 5, 8)]}
        _bars, _fetch, daily_rows, daily_fetch = loader.fetch_contract(
            Client(),
            contract,
            date(2024, 10, 1),
            date(2024, 10, 18),
            lookup,
            date(2024, 10, 4),
        )
        self.assertEqual(daily_fetch["source"], "flatfile")
        self.assertEqual(sorted(row["bar_date"] for row in daily_rows), ["2024-10-04", "2024-10-07"])
        self.assertTrue(any("/range/1/day/" in url for url in calls))

    def test_flatfile_keys_keep_days_inside_the_window(self):
        class Pages:
            def paginate(self, Bucket, Prefix):
                yield {
                    "Contents": [
                        {"Key": f"{Prefix}2024-10-04.csv.gz"},
                        {"Key": f"{Prefix}2024-10-03.csv.gz"},
                        {"Key": f"{Prefix}notes.txt"},
                    ]
                }

        class Client:
            def get_paginator(self, _name):
                return Pages()

        keys = loader.flatfile_keys(Client(), date(2024, 10, 4), date(2024, 10, 4))
        self.assertEqual([day for day, _key in keys], [date(2024, 10, 4)])


if __name__ == "__main__":
    unittest.main()
