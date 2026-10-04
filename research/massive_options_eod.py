#!/usr/bin/env python3
"""Load two years of hourly option bars from Massive into DuckDB.

Every active and expired contract on the selected underlyings is kept.
Daily bars come from Massive flat files when S3 keys are configured, and
otherwise from one REST request per contract. Hourly requests then cover
only the days that traded, over reused connections. A contract that fails
is left unrecorded so the next run retries it. A stopped run lists
contracts again, then skips tickers already in option_hour_fetches.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import random
import sys
import threading
import time
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = Path(__file__).resolve().parent
DB_PATH = RESEARCH / "massive_options.duckdb"
BASE = "https://api.massive.com"
FLAT_ENDPOINT = "https://files.massive.com"
FLAT_BUCKET = "flatfiles"
FLAT_PREFIX = "us_options_opra/day_aggs_v1"
FLAT_CACHE = RESEARCH / ".dlt" / "flatfiles"
UNDERLYINGS = ("WULF", "NBIS", "IREN", "CIFR")
EASTERN = ZoneInfo("America/New_York")
WORKERS = 32
FLUSH_EVERY = 250
FLUSH_SECONDS = 60
MINUTE_BUDGET = 45_000
SESSION_MINUTES = 960
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60


def window_start(today: date) -> date:
    try:
        return today.replace(year=today.year - 2)
    except ValueError:
        return today.replace(year=today.year - 2, day=28)


def redact(text: str, *secrets: str) -> str:
    redacted = text
    for secret in (os.environ.get("MASSIVE_API_KEY", ""), *secrets):
        if secret:
            redacted = redacted.replace(secret, "REDACTED")
    return redacted


class MassiveClient:
    def __init__(self, api_key: str, session_factory=None):
        self.api_key = api_key
        self._local = threading.local()
        self._session_factory = session_factory or self._default_session

    @staticmethod
    def _default_session() -> requests.Session:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            self._local.session = session
        return session

    def _drop_session(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
        self._local.session = None

    def _with_key(self, url: str, params: dict | None) -> str:
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        if params:
            query.update({key: str(value) for key, value in params.items()})
        query["apiKey"] = self.api_key
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
        )

    def get(self, url: str, params: dict | None = None) -> dict:
        last_error = "request failed"
        for attempt in range(6):
            try:
                response = self._session().get(
                    self._with_key(url, params),
                    headers={"Accept": "application/json"},
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._drop_session()
                last_error = f"network: {exc}"
                self._wait(min(30, 2**attempt), last_error, attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:240]}"
                delay = 15 if response.status_code == 429 else min(30, 2**attempt)
                self._wait(delay, last_error, attempt)
                continue
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}: {response.text[:240]}"
                raise RuntimeError(redact(last_error, self.api_key))
            try:
                payload = response.json()
            except ValueError as exc:
                last_error = f"invalid json: {exc}"
                self._wait(min(30, 2**attempt), last_error, attempt)
                continue
            status = str(payload.get("status") or "OK")
            if status not in {"OK", "DELAYED"}:
                message = str(payload.get("error") or payload.get("message") or status)
                last_error = message
                if "maximum requests" in message.lower():
                    self._wait(15, message, attempt)
                    continue
                raise RuntimeError(redact(message, self.api_key))
            return payload
        raise RuntimeError(redact(last_error, self.api_key))

    def _wait(self, seconds: float, reason: str, attempt: int) -> None:
        if attempt >= 5:
            return
        delay = seconds + random.random()
        print(
            f"retrying after {redact(reason, self.api_key)}; sleeping {delay:.0f}s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)

    def pages(self, path: str, params: dict):
        url = BASE + path
        query: dict | None = params
        while url:
            payload = self.get(url, query)
            query = None
            yield payload
            url = payload.get("next_url") or None


def contract_row(raw: dict, expired: bool) -> dict:
    extra = raw.get("additional_underlyings")
    return {
        "ticker": raw["ticker"],
        "underlying_ticker": raw.get("underlying_ticker"),
        "contract_type": raw.get("contract_type"),
        "exercise_style": raw.get("exercise_style"),
        "expiration_date": raw.get("expiration_date"),
        "strike_price": raw.get("strike_price"),
        "primary_exchange": raw.get("primary_exchange"),
        "shares_per_contract": raw.get("shares_per_contract"),
        "cfi": raw.get("cfi"),
        "correction": raw.get("correction"),
        "additional_underlyings": json.dumps(extra) if extra else None,
        "expired": expired,
    }


def collect_contracts(
    client: MassiveClient,
    start: date,
    limit: int | None,
    underlyings: tuple[str, ...],
) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for underlying in underlyings:
        for expired in (True, False):
            params = {
                "underlying_ticker": underlying,
                "expired": "true" if expired else "false",
                "expiration_date.gte": start.isoformat(),
                "limit": 1000,
                "sort": "expiration_date",
                "order": "asc",
            }
            for page_no, payload in enumerate(client.pages("/v3/reference/options/contracts", params), start=1):
                results = payload.get("results") or []
                kept = 0
                for raw in results:
                    ticker = raw["ticker"]
                    if ticker in seen:
                        continue
                    seen.add(ticker)
                    found.append(contract_row(raw, expired))
                    kept += 1
                    if limit is not None and len(found) >= limit:
                        print(
                            f"kept {len(found)} contracts (--max-contracts)",
                            file=sys.stderr,
                            flush=True,
                        )
                        return found
                print(
                    f"{underlying} expired={expired} page {page_no}: "
                    f"{len(results)} listed, {kept} new, total {len(found)}",
                    file=sys.stderr,
                    flush=True,
                )
    return found


def aggregate_bars(
    client: MassiveClient, ticker: str, timespan: str, start: date, end: date
) -> tuple[list[dict], bool]:
    encoded = urllib.parse.quote(ticker, safe="")
    path = f"/v2/aggs/ticker/{encoded}/range/1/{timespan}/{start.isoformat()}/{end.isoformat()}"
    params: dict | None = {"adjusted": "true", "sort": "asc", "limit": 50000}
    rows: list[dict] = []
    adjusted = True
    url = BASE + path
    first = True
    while url:
        payload = client.get(url, params)
        params = None
        if first:
            adjusted = bool(payload.get("adjusted", True))
            first = False
        rows.extend(payload.get("results") or [])
        url = payload.get("next_url") or None
    return rows, adjusted


def hourly_bars(client: MassiveClient, ticker: str, start: date, end: date) -> tuple[list[dict], bool]:
    return aggregate_bars(client, ticker, "hour", start, end)


def daily_bars(client: MassiveClient, ticker: str, start: date, end: date) -> tuple[list[dict], bool]:
    return aggregate_bars(client, ticker, "day", start, end)


def bar_day(raw: dict) -> date:
    return datetime.fromtimestamp(int(raw["t"]) / 1000, tz=EASTERN).date()


def minute_cost(raw: dict) -> int:
    trades = raw.get("n")
    if trades is None:
        return SESSION_MINUTES
    return min(max(int(trades), 0), SESSION_MINUTES)


def plan_windows(days: list[dict]) -> list[list[dict]]:
    windows: list[list[dict]] = []
    current: list[dict] = []
    budget = 0
    for raw in days:
        cost = minute_cost(raw)
        if current and budget + cost > MINUTE_BUDGET:
            windows.append(current)
            current = []
            budget = 0
        current.append(raw)
        budget += cost
    if current:
        windows.append(current)
    return windows


def volume_total(rows: list[dict]) -> float:
    return sum(float(row.get("v") or 0) for row in rows)


def window_empty(hourly: list[dict], days: list[dict]) -> bool:
    return not hourly and volume_total(days) > 0


def day_mismatches(hourly: list[dict], days: list[dict]) -> int:
    hourly_by_day: dict[date, float] = {}
    for raw in hourly:
        day = bar_day(raw)
        hourly_by_day[day] = hourly_by_day.get(day, 0.0) + float(raw.get("v") or 0)
    mismatches = 0
    for raw in days:
        daily_volume = float(raw.get("v") or 0)
        hourly_volume = hourly_by_day.get(bar_day(raw), 0.0)
        if abs(hourly_volume - daily_volume) >= 0.5:
            mismatches += 1
    return mismatches


def hour_row(contract: dict, raw: dict, adjusted: bool) -> dict:
    timestamp = int(raw["t"])
    bar_start = datetime.fromtimestamp(timestamp / 1000, tz=EASTERN).isoformat(timespec="seconds")
    return {
        "option_ticker": contract["ticker"],
        "underlying_ticker": contract["underlying_ticker"],
        "expiration_date": contract["expiration_date"],
        "strike_price": contract["strike_price"],
        "contract_type": contract["contract_type"],
        "t": timestamp,
        "bar_start": bar_start,
        "open": raw.get("o"),
        "high": raw.get("h"),
        "low": raw.get("l"),
        "close": raw.get("c"),
        "volume": raw.get("v"),
        "vwap": raw.get("vw"),
        "transactions": raw.get("n"),
        "adjusted": adjusted,
    }


def bar_row(contract: dict, raw: dict, adjusted: bool) -> dict:
    timestamp = int(raw["t"])
    return {
        "option_ticker": contract["ticker"],
        "underlying_ticker": contract["underlying_ticker"],
        "expiration_date": contract["expiration_date"],
        "strike_price": contract["strike_price"],
        "contract_type": contract["contract_type"],
        "t": timestamp,
        "bar_date": bar_day(raw).isoformat(),
        "open": raw.get("o"),
        "high": raw.get("h"),
        "low": raw.get("l"),
        "close": raw.get("c"),
        "volume": raw.get("v"),
        "vwap": raw.get("vw"),
        "transactions": raw.get("n"),
        "adjusted": adjusted,
    }


def fetch_window(client: MassiveClient, contract: dict, days: list[dict], stats: dict) -> list[dict]:
    stats["hour_requests"] += 1
    raw_bars, adjusted = hourly_bars(client, contract["ticker"], bar_day(days[0]), bar_day(days[-1]))
    if window_empty(raw_bars, days) and len(days) > 1:
        middle = len(days) // 2
        return fetch_window(client, contract, days[:middle], stats) + fetch_window(
            client, contract, days[middle:], stats
        )
    stats["volume_mismatch_days"] += day_mismatches(raw_bars, days)
    return [hour_row(contract, raw, adjusted) for raw in raw_bars]


def _normalize_daily(raw_rows: list[dict]) -> list[dict]:
    deduped = {int(raw["t"]): raw for raw in raw_rows}
    return [deduped[stamp] for stamp in sorted(deduped)]


def fetch_contract(
    client: MassiveClient,
    contract: dict,
    start: date,
    today: date,
    daily_lookup: dict[str, list[dict]] | None = None,
    flat_through: date | None = None,
) -> tuple[list[dict], dict, list[dict], dict]:
    expiration = date.fromisoformat(str(contract["expiration_date"])[:10])
    end = min(today, expiration)
    source = "rest"
    if daily_lookup is None:
        raw_daily, daily_adjusted = daily_bars(client, contract["ticker"], start, end)
        for raw in raw_daily:
            raw["_adjusted"] = daily_adjusted
    else:
        raw_daily = []
        for raw in daily_lookup.get(contract["ticker"], []):
            if start <= bar_day(raw) <= end:
                row = dict(raw)
                row["_adjusted"] = False
                raw_daily.append(row)
        flat_count = len(raw_daily)
        source = "flatfile" if flat_count else "rest"
        if flat_through is not None and end > flat_through:
            gap_start = flat_through + timedelta(days=1)
            if gap_start <= end:
                extra, adjusted = daily_bars(client, contract["ticker"], gap_start, end)
                for raw in extra:
                    raw["_adjusted"] = adjusted
                raw_daily.extend(extra)
                if extra and not flat_count:
                    source = "rest"
    raw_daily = _normalize_daily(raw_daily)
    daily_rows = [bar_row(contract, raw, bool(raw.get("_adjusted", False))) for raw in raw_daily]
    fetched_at = datetime.now(EASTERN).isoformat(timespec="seconds")
    daily_fetch = {
        "option_ticker": contract["ticker"],
        "results_count": len(daily_rows),
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "fetched_at": fetched_at,
        "source": source,
    }
    stats = {"hour_requests": 0, "volume_mismatch_days": 0}
    bars: list[dict] = []
    for window in plan_windows(raw_daily):
        bars.extend(fetch_window(client, contract, window, stats))
    bars = list({row["t"]: row for row in bars}.values())
    fetch = {
        "option_ticker": contract["ticker"],
        "results_count": len(bars),
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "fetched_at": fetched_at,
        "daily_days": len(daily_rows),
        "hour_requests": stats["hour_requests"],
        "volume_mismatch_days": stats["volume_mismatch_days"],
    }
    return bars, fetch, daily_rows, daily_fetch


def eastern_midnight_millis(millis: int) -> int:
    moment = datetime.fromtimestamp(millis / 1000, tz=EASTERN)
    midnight = datetime.combine(moment.date(), datetime.min.time(), tzinfo=EASTERN)
    return int(midnight.timestamp() * 1000)


def _number(value: str | None, whole: bool) -> float | int | None:
    if value is None or value == "":
        return None
    parsed = float(value)
    if whole:
        return int(parsed)
    return parsed


def flat_bar(record: dict) -> dict:
    millis = eastern_midnight_millis(int(record["window_start"]) // 1_000_000)
    return {
        "t": millis,
        "o": _number(record.get("open"), False),
        "h": _number(record.get("high"), False),
        "l": _number(record.get("low"), False),
        "c": _number(record.get("close"), False),
        "v": _number(record.get("volume"), False),
        "n": _number(record.get("transactions"), True),
        "vw": None,
    }


def parse_day_aggregates(payload: bytes, wanted: set[str]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as handle:
        reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8"))
        for record in reader:
            ticker = record.get("ticker") or ""
            if ticker not in wanted:
                continue
            rows[ticker] = flat_bar(record)
    return rows


def flatfiles_enabled(force_rest: bool) -> bool:
    if force_rest:
        return False
    access = os.environ.get("MASSIVE_S3_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("MASSIVE_S3_SECRET_ACCESS_KEY", "").strip()
    return bool(access and secret)


def flatfile_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=FLAT_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id=os.environ["MASSIVE_S3_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["MASSIVE_S3_SECRET_ACCESS_KEY"].strip(),
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def flatfile_keys(client, start: date, end: date) -> list[tuple[date, str]]:
    found: list[tuple[date, str]] = []
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cursor <= last:
        prefix = f"{FLAT_PREFIX}/{cursor.year:04d}/{cursor.month:02d}/"
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=FLAT_BUCKET, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                name = key.rsplit("/", 1)[-1]
                if not name.endswith(".csv.gz") or len(name) < 10:
                    continue
                try:
                    day = date.fromisoformat(name[:10])
                except ValueError:
                    continue
                if start <= day <= end:
                    found.append((day, key))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    found.sort()
    return found


def _filter_digest(wanted: set[str]) -> str:
    digest = hashlib.sha256("\n".join(sorted(wanted)).encode()).hexdigest()
    return digest[:16]


def _cache_path(day: date, wanted: set[str]) -> Path:
    return FLAT_CACHE / f"{day.isoformat()}.{_filter_digest(wanted)}.json.gz"


def _read_cached_day(day: date, wanted: set[str]) -> dict[str, dict] | None:
    path = _cache_path(day, wanted)
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    return {item["ticker"]: item["bar"] for item in payload}


def _write_cached_day(day: date, wanted: set[str], rows: dict[str, dict]) -> None:
    FLAT_CACHE.mkdir(parents=True, exist_ok=True)
    payload = [{"ticker": ticker, "bar": bar} for ticker, bar in rows.items()]
    with gzip.open(_cache_path(day, wanted), "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _download_day(client, day: date, key: str, wanted: set[str]) -> dict[str, dict]:
    cached = _read_cached_day(day, wanted)
    if cached is not None:
        return cached
    body = client.get_object(Bucket=FLAT_BUCKET, Key=key)["Body"].read()
    rows = parse_day_aggregates(body, wanted)
    _write_cached_day(day, wanted, rows)
    return rows


def load_daily_lookup(
    client,
    contracts: list[dict],
    start: date,
    today: date,
) -> tuple[dict[str, list[dict]], date | None]:
    wanted = {contract["ticker"] for contract in contracts}
    keys = flatfile_keys(client, start, today)
    if not keys:
        return {}, None
    lookup: dict[str, list[dict]] = {}

    def load_one(item: tuple[date, str]) -> tuple[date, dict[str, dict]]:
        day, key = item
        return day, _download_day(client, day, key, wanted)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for day, rows in pool.map(load_one, keys):
            print(f"flatfile {day.isoformat()} {len(rows)} contracts", file=sys.stderr, flush=True)
            for ticker, bar in rows.items():
                lookup.setdefault(ticker, []).append(bar)
    for bars in lookup.values():
        bars.sort(key=lambda raw: int(raw["t"]))
    return lookup, keys[-1][0]


def relation(connection, table: str) -> str | None:
    rows = connection.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE lower(table_name) = ?
        """,
        [table.lower()],
    ).fetchall()
    if not rows:
        return None
    schema, name = next(
        ((item_schema, item_name) for item_schema, item_name in rows if item_schema == "massive"),
        rows[0],
    )
    return f'"{schema}"."{name}"'


def fetched_tickers() -> set[str]:
    if not DB_PATH.exists():
        return set()
    import duckdb

    connection = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        quoted = relation(connection, "option_hour_fetches")
        if quoted is None:
            return set()
        return {row[0] for row in connection.execute(f"SELECT option_ticker FROM {quoted}").fetchall()}
    finally:
        connection.close()


def print_summary() -> None:
    if not DB_PATH.exists():
        print("duckdb: missing")
        return
    import duckdb

    connection = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        for table in (
            "option_contracts",
            "option_hour_bars",
            "option_hour_fetches",
            "option_bars",
            "option_bar_fetches",
        ):
            quoted = relation(connection, table)
            if quoted is None:
                print(f"{table}: missing")
                continue
            count = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            print(f"{table}: {count}")
        bars = relation(connection, "option_hour_bars")
        if bars:
            sample = connection.execute(
                f"SELECT option_ticker, bar_start, close FROM {bars} ORDER BY bar_start LIMIT 5"
            ).fetchall()
            for ticker, bar_start, close in sample:
                print(f"sample {ticker} {bar_start} close={close}")
        fetches = relation(connection, "option_hour_fetches")
        if fetches:
            recent = connection.execute(
                f"""
                SELECT option_ticker, results_count, daily_days, hour_requests, volume_mismatch_days
                FROM {fetches}
                ORDER BY fetched_at DESC
                LIMIT 5
                """
            ).fetchall()
            for ticker, count, days, requests, mismatches in recent:
                print(
                    f"fetch {ticker} rows={count} days={days} requests={requests} mismatches={mismatches}"
                )
    finally:
        connection.close()


def pipeline():
    import dlt

    return dlt.pipeline(
        pipeline_name="massive_options_eod",
        destination=dlt.destinations.duckdb(str(DB_PATH)),
        dataset_name="massive",
        pipelines_dir=str(RESEARCH / ".dlt" / "pipelines"),
    )


def load_rows(pipe, specs: list[tuple[str, object, list[dict]]]) -> None:
    import dlt

    resources = []
    for name, primary_key, rows in specs:
        if not rows:
            continue

        def make(rows=rows, name=name, primary_key=primary_key):
            @dlt.resource(
                name=name,
                write_disposition="merge",
                primary_key=primary_key,
                max_table_nesting=0,
            )
            def resource():
                yield from rows

            return resource()

        resources.append(make())
    if resources:
        pipe.run(resources)


def download_hours(
    client: MassiveClient,
    pipe,
    pending: list[dict],
    start: date,
    today: date,
    workers: int,
    daily_lookup: dict[str, list[dict]] | None = None,
    flat_through: date | None = None,
) -> int:
    if not pending:
        print("failed: 0", flush=True)
        return 0
    batch_bars: list[dict] = []
    batch_fetches: list[dict] = []
    batch_daily: list[dict] = []
    batch_daily_fetches: list[dict] = []
    completed = 0
    failed = 0
    consecutive = 0
    failure_limit = 3 * workers
    last_flush = time.monotonic()

    def flush() -> None:
        nonlocal batch_bars, batch_fetches, batch_daily, batch_daily_fetches, last_flush
        if not batch_fetches:
            return
        count = len(batch_fetches)
        started = time.monotonic()
        load_rows(
            pipe,
            [
                ("option_hour_bars", ["option_ticker", "t"], batch_bars),
                ("option_bars", ["option_ticker", "t"], batch_daily),
            ],
        )
        load_rows(
            pipe,
            [
                ("option_hour_fetches", "option_ticker", batch_fetches),
                ("option_bar_fetches", "option_ticker", batch_daily_fetches),
            ],
        )
        print(f"flushed {count} contracts in {time.monotonic() - started:.1f}s", file=sys.stderr, flush=True)
        batch_bars = []
        batch_fetches = []
        batch_daily = []
        batch_daily_fetches = []
        last_flush = time.monotonic()

    contracts = iter(pending)
    stopped = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        inflight: dict = {}

        def submit_next() -> bool:
            try:
                contract = next(contracts)
            except StopIteration:
                return False
            future = pool.submit(
                fetch_contract, client, contract, start, today, daily_lookup, flat_through
            )
            inflight[future] = contract
            return True

        for _ in range(workers * 2):
            if not submit_next():
                break
        while inflight and not stopped:
            finished, _pending = wait(set(inflight), return_when=FIRST_COMPLETED)
            for future in finished:
                contract = inflight.pop(future)
                try:
                    bars, fetch, daily_rows, daily_fetch = future.result()
                except Exception as exc:
                    failed += 1
                    consecutive += 1
                    print(
                        f"failed {contract['ticker']}: {redact(str(exc), client.api_key)}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if consecutive >= failure_limit:
                        stopped = True
                        for leftover in list(inflight):
                            leftover.cancel()
                        inflight.clear()
                        break
                    submit_next()
                    continue
                consecutive = 0
                batch_bars.extend(bars)
                batch_fetches.append(fetch)
                batch_daily.extend(daily_rows)
                batch_daily_fetches.append(daily_fetch)
                completed += 1
                print(
                    f"hours {completed}/{len(pending)} {fetch['option_ticker']} "
                    f"{fetch['results_count']} rows days={fetch['daily_days']} "
                    f"requests={fetch['hour_requests']} mismatches={fetch['volume_mismatch_days']}",
                    file=sys.stderr,
                    flush=True,
                )
                submit_next()
            now = time.monotonic()
            if batch_fetches and (len(batch_fetches) >= FLUSH_EVERY or now - last_flush >= FLUSH_SECONDS):
                flush()
    flush()
    print(f"failed: {failed}", flush=True)
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-contracts",
        type=int,
        default=None,
        help="Stop after this many contracts. Used to smoke-test the load.",
    )
    parser.add_argument(
        "--underlying",
        action="append",
        choices=UNDERLYINGS,
        help="Limit the load to this underlying. Repeat to select more than one.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=WORKERS,
        help="How many contracts to download at once.",
    )
    parser.add_argument(
        "--no-flatfiles",
        action="store_true",
        help="Load daily bars from the REST API even when S3 keys are set.",
    )
    args = parser.parse_args()
    if args.max_contracts is not None and args.max_contracts < 1:
        print("--max-contracts must be positive", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("--workers must be positive", file=sys.stderr)
        return 2

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not api_key:
        print("MASSIVE_API_KEY is missing from .env", file=sys.stderr)
        return 2

    os.chdir(RESEARCH)
    today = date.today()
    start = window_start(today)
    underlyings = tuple(dict.fromkeys(args.underlying)) if args.underlying else UNDERLYINGS
    print(
        f"window {start.isoformat()} .. {today.isoformat()} underlyings {', '.join(underlyings)} -> {DB_PATH.name}",
        file=sys.stderr,
        flush=True,
    )

    client = MassiveClient(api_key)
    done = fetched_tickers()
    contracts = collect_contracts(client, start, args.max_contracts, underlyings)
    print(f"contracts: {len(contracts)}", flush=True)
    pipe = pipeline()
    load_rows(pipe, [("option_contracts", "ticker", contracts)])

    daily_lookup = None
    flat_through = None
    if flatfiles_enabled(args.no_flatfiles):
        try:
            daily_lookup, flat_through = load_daily_lookup(flatfile_client(), contracts, start, today)
            covered = len(daily_lookup)
            through = flat_through.isoformat() if flat_through else "none"
            print(f"flat files through {through} covering {covered} contracts", flush=True)
        except Exception as exc:
            print(
                f"flat files unavailable ({redact(str(exc), api_key)}); using REST daily bars",
                file=sys.stderr,
                flush=True,
            )
            daily_lookup = None
            flat_through = None

    pending = [contract for contract in contracts if contract["ticker"] not in done]
    if args.max_contracts is not None:
        pending = pending[: args.max_contracts]
    print(f"hour fetches pending: {len(pending)} (already fetched {len(done)})", flush=True)
    failed = download_hours(
        client, pipe, pending, start, today, args.workers, daily_lookup, flat_through
    )
    print_summary()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
