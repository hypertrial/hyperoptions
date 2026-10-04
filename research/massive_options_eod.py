#!/usr/bin/env python3
"""Load two years of hourly option bars from Massive into DuckDB.

Every active and expired contract on the selected underlyings is kept.
Hourly aggregates are requested in 30-day chunks so the minute scan budget
cannot drop a window silently. A stopped run lists contracts again, then
continues at the first ticker not already in option_hour_fetches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = Path(__file__).resolve().parent
DB_PATH = RESEARCH / "massive_options.duckdb"
BASE = "https://api.massive.com"
UNDERLYINGS = ("WULF", "NBIS", "IREN", "CIFR")
EASTERN = ZoneInfo("America/New_York")
WORKERS = 8
FLUSH_EVERY = 25
CHUNK_DAYS = 30


def window_start(today: date) -> date:
    try:
        return today.replace(year=today.year - 2)
    except ValueError:
        return today.replace(year=today.year - 2, day=28)


def redact(text: str) -> str:
    return text.replace(os.environ.get("MASSIVE_API_KEY", ""), "REDACTED")


class MassiveClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

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
            request = urllib.request.Request(
                self._with_key(url, params),
                headers={"Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                last_error = f"HTTP {exc.code}: {body[:240]}"
                if exc.code == 429 or exc.code >= 500:
                    self._wait(15 if exc.code == 429 else min(30, 2**attempt), last_error, attempt)
                    continue
                raise RuntimeError(redact(last_error)) from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                last_error = f"network: {exc}"
                self._wait(min(30, 2**attempt), last_error, attempt)
                continue
            status = str(payload.get("status") or "OK")
            if status not in {"OK", "DELAYED"}:
                message = str(payload.get("error") or payload.get("message") or status)
                last_error = message
                if "maximum requests" in message.lower():
                    self._wait(15, message, attempt)
                    continue
                raise RuntimeError(redact(message))
            return payload
        raise RuntimeError(redact(last_error))

    def _wait(self, seconds: float, reason: str, attempt: int) -> None:
        if attempt >= 5:
            return
        print(f"retrying after {redact(reason)}; sleeping {seconds:.0f}s", file=sys.stderr, flush=True)
        time.sleep(seconds)

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


def hour_chunks(start: date, end: date):
    cursor_end = end
    while cursor_end >= start:
        cursor_start = max(start, cursor_end - timedelta(days=CHUNK_DAYS - 1))
        yield cursor_start, cursor_end
        if cursor_start <= start:
            break
        cursor_end = cursor_start - timedelta(days=1)


def hourly_bars(client: MassiveClient, ticker: str, start: date, end: date) -> tuple[list[dict], bool]:
    encoded = urllib.parse.quote(ticker, safe="")
    path = f"/v2/aggs/ticker/{encoded}/range/1/hour/{start.isoformat()}/{end.isoformat()}"
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


def fetch_contract(client: MassiveClient, contract: dict, start: date, today: date) -> tuple[list[dict], dict]:
    expiration = date.fromisoformat(contract["expiration_date"])
    end = min(today, expiration)
    bars: list[dict] = []
    adjusted = True
    for chunk_start, chunk_end in hour_chunks(start, end):
        raw_bars, adjusted = hourly_bars(client, contract["ticker"], chunk_start, chunk_end)
        bars.extend(hour_row(contract, raw, adjusted) for raw in raw_bars)
    bars = list({row["t"]: row for row in bars}.values())
    fetch = {
        "option_ticker": contract["ticker"],
        "results_count": len(bars),
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "fetched_at": datetime.now(EASTERN).isoformat(timespec="seconds"),
    }
    return bars, fetch


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
        for table in ("option_contracts", "option_hour_bars", "option_hour_fetches"):
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


def download_hours(client: MassiveClient, pipe, pending: list[dict], start: date, today: date) -> None:
    if not pending:
        return
    batch_bars: list[dict] = []
    batch_fetches: list[dict] = []
    completed = 0

    def flush() -> None:
        nonlocal batch_bars, batch_fetches
        if not batch_fetches:
            return
        load_rows(
            pipe,
            [
                ("option_hour_bars", ["option_ticker", "t"], batch_bars),
                ("option_hour_fetches", "option_ticker", batch_fetches),
            ],
        )
        batch_bars = []
        batch_fetches = []

    contracts = iter(pending)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        inflight = set()

        def submit_next() -> bool:
            try:
                contract = next(contracts)
            except StopIteration:
                return False
            inflight.add(pool.submit(fetch_contract, client, contract, start, today))
            return True

        for _ in range(WORKERS):
            if not submit_next():
                break
        while inflight:
            finished, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for future in finished:
                bars, fetch = future.result()
                batch_bars.extend(bars)
                batch_fetches.append(fetch)
                completed += 1
                print(
                    f"hours {completed}/{len(pending)} {fetch['option_ticker']} {fetch['results_count']} rows",
                    file=sys.stderr,
                    flush=True,
                )
                submit_next()
            if len(batch_fetches) >= FLUSH_EVERY:
                flush()
    flush()


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
    args = parser.parse_args()
    if args.max_contracts is not None and args.max_contracts < 1:
        print("--max-contracts must be positive", file=sys.stderr)
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

    pending = [contract for contract in contracts if contract["ticker"] not in done]
    if args.max_contracts is not None:
        pending = pending[: args.max_contracts]
    print(f"hour fetches pending: {len(pending)} (already fetched {len(done)})", flush=True)
    download_hours(client, pipe, pending, start, today)
    print_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
