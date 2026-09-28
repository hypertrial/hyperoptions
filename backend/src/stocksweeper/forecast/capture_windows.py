"""Append-only denominators for expected and missed intraday watch captures."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import duckdb

from options_api.contract_identity import parse_watch_key
from options_api.market_calendar import _calendar
from stocksweeper.storage.db import connect, rows

_NY = ZoneInfo("America/New_York")
_WINDOWS = {"10:00": time(10), "13:00": time(13), "15:30": time(15, 30)}
_GRACE = timedelta(minutes=5)
_LOOKBACK_DAYS = 35


def capture_window_counts(data_dir: Path, since: datetime, as_of: datetime) -> dict[str, int]:
    """Read distinct expected windows, with later captures superseding misses.

    The 35-day limit matches restart reconciliation. This reads existing events
    without creating a database or changing the append-only event log.
    """
    if (
        since.tzinfo is None
        or as_of.tzinfo is None
        or not timedelta(0) <= as_of - since <= timedelta(days=_LOOKBACK_DAYS)
    ):
        raise ValueError("capture count interval must be aware and at most 35 days")
    counts = {"expected": 0, "captured": 0, "missed": 0, "pending": 0}
    path = data_dir / "results.duckdb"
    if not path.is_file():
        return counts
    # DuckDB cannot mix read-only and read-write connections for one file in
    # the running app. Use its normal connection mode but issue SELECTs only.
    with duckdb.connect(str(path)) as connection:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if "forecast_capture_window_events" not in tables:
            return counts
        events = rows(
            connection,
            """SELECT contract_key, target_at, event, recorded_at
               FROM forecast_capture_window_events
               WHERE target_at >= ? AND target_at <= ? AND recorded_at <= ?""",
            [since, as_of, as_of],
        )
    observed: dict[tuple[str, datetime], set[str]] = {}
    for row in events:
        key = (row["contract_key"], row["target_at"].astimezone(UTC))
        observed.setdefault(key, set()).add(row["event"])
    for (_, target), states in observed.items():
        if "expected" not in states:
            continue
        counts["expected"] += 1
        state = (
            "captured"
            if "captured" in states
            else ("missed" if "missed" in states or target + _GRACE <= as_of else "pending")
        )
        counts[state] += 1
    return counts


class WatchedContract(Protocol):
    watch_key: str
    created_at: datetime
    expiration: date


def reconcile_capture_windows(
    data_dir: Path,
    watches: Iterable[WatchedContract],
    now: datetime,
    *,
    contract_key: str | None = None,
) -> dict[str, int]:
    """Record expected windows and outcomes for the current watchlist.

    Call at startup and after every window. Restarts recover up to 35 calendar
    days of missed windows for watches still present. Deleted watches cannot be
    reconstructed from the current table and are deliberately excluded.
    """
    if now.tzinfo is None:
        raise ValueError("capture reconciliation time must be timezone-aware")
    if contract_key is not None and parse_watch_key(contract_key) is None:
        raise ValueError("invalid contract key for capture accounting")
    now = now.astimezone(UTC)
    current_day = now.astimezone(_NY).date()
    calendar = _calendar()
    start = current_day - timedelta(days=_LOOKBACK_DAYS)
    sessions = calendar.sessions_in_range(start.isoformat(), current_day.isoformat())
    targets: dict[tuple[str, datetime], str] = {}
    for watch in watches:
        if contract_key is not None and watch.watch_key != contract_key:
            continue
        if parse_watch_key(watch.watch_key) is None or watch.created_at.tzinfo is None:
            raise ValueError("invalid watched contract for capture accounting")
        created_at = watch.created_at.astimezone(UTC)
        for session in sessions:
            day = session.date()
            if day > watch.expiration:
                break
            opened = calendar.session_open(session).to_pydatetime()
            closed = calendar.session_close(session).to_pydatetime()
            for window, wall_time in _WINDOWS.items():
                target = datetime.combine(day, wall_time, _NY).astimezone(UTC)
                if opened <= target < closed and created_at <= target <= now:
                    targets[(watch.watch_key, target)] = window
    with connect(data_dir / "results.duckdb") as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS forecast_capture_window_events (
                event_key VARCHAR PRIMARY KEY,
                contract_key VARCHAR NOT NULL,
                target_at TIMESTAMPTZ NOT NULL,
                snapshot_window VARCHAR NOT NULL,
                event VARCHAR NOT NULL,
                reason VARCHAR,
                recorded_at TIMESTAMPTZ NOT NULL
            )"""
        )
        # A watch may be removed after its target but before the grace period
        # ends. Keep reconciling already-recorded expectations after removal.
        persisted_filter = " AND contract_key = ?" if contract_key is not None else ""
        persisted_params = [datetime.combine(start, time.min, _NY).astimezone(UTC), now]
        if contract_key is not None:
            persisted_params.append(contract_key)
        persisted = rows(
            connection,
            """SELECT contract_key, target_at, snapshot_window
               FROM forecast_capture_window_events
               WHERE event = 'expected' AND target_at >= ? AND target_at <= ?"""
            + persisted_filter,
            persisted_params,
        )
        for row in persisted:
            window = row["snapshot_window"]
            if window in _WINDOWS:
                targets[(row["contract_key"], row["target_at"].astimezone(UTC))] = window
        if not targets:
            return {"expected": 0, "captured": 0, "missed": 0, "pending": 0}
        earliest = min(target for _, target in targets)
        issued_filter = " AND contract_key = ?" if contract_key is not None else ""
        issued_params = [earliest, now]
        if contract_key is not None:
            issued_params.append(contract_key)
        issued = rows(
            connection,
            """SELECT DISTINCT contract_key, issued_at, snapshot_window
               FROM forecast_issuances
               WHERE snapshot_window IS NOT NULL AND issued_at >= ? AND issued_at <= ?"""
            + issued_filter,
            issued_params,
        )
        captured = set()
        for row in issued:
            window = row["snapshot_window"]
            if window not in _WINDOWS:
                continue
            issued_at = row["issued_at"].astimezone(UTC)
            day = issued_at.astimezone(_NY).date()
            target = datetime.combine(day, _WINDOWS[window], _NY).astimezone(UTC)
            key = (row["contract_key"], target)
            if key in targets and target <= issued_at < target + _GRACE:
                captured.add(key)
        counts = {"expected": len(targets), "captured": 0, "missed": 0, "pending": 0}
        new_events = []
        for (key, target), window in targets.items():
            state = (
                "captured"
                if (key, target) in captured
                else ("missed" if target + _GRACE <= now else "pending")
            )
            counts[state] += 1
            for event, reason in (
                ("expected", None),
                (state, "no_issuance_in_window" if state == "missed" else None),
            ):
                if event == "pending":
                    continue
                event_key = hashlib.sha256(
                    f"{key}|{target.isoformat()}|{event}".encode()
                ).hexdigest()
                new_events.append((event_key, key, target, window, event, reason, now))
        connection.executemany(
            """INSERT OR IGNORE INTO forecast_capture_window_events
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            new_events,
        )
        return counts
