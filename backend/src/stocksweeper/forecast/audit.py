"""Freeze a fixed, current-vintage Nasdaq audit cohort for reproducible replay.

These snapshots are immutable after creation. Historical forecasts reconstructed
from them are retrospective screening evidence, never prospective as-issued data.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import polars as pl

from options_api.models import normalize_ticker
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import (
    CacheIntegrityError,
    ForecastPriceStore,
    YahooForecastProvider,
    clean_completed,
    price_hash,
)

AUDIT_SIZE = 50


@dataclass(frozen=True)
class AuditMember:
    ticker: str
    data_hash: str
    source_retrieved_at: datetime
    snapshot: Path


@dataclass(frozen=True)
class AuditCohort:
    completed_session: date
    frozen_at: datetime
    members: tuple[AuditMember, ...]
    provenance: str = "immutable_current_vintage_replay"


def _write_once(path: Path, write: Callable[[Path], object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        write(temporary)
        temporary.chmod(0o400)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def read_audit_cohort(data_dir: Path) -> AuditCohort | None:
    root = data_dir / "forecast" / "audit"
    manifest = root / "cohort.json"
    if not manifest.exists():
        return None
    try:
        metadata = json.loads(manifest.read_text())
        if metadata["provenance"] != "immutable_current_vintage_replay":
            raise ValueError("incorrect audit provenance")
        members = []
        for item in metadata["members"]:
            ticker = item["ticker"]
            digest = item["data_hash"]
            if normalize_ticker(ticker) != ticker or len(digest) != 64:
                raise ValueError("invalid audit member")
            path = root / "snapshots" / f"{ticker}-{digest}.parquet"
            frame = pl.read_parquet(path)
            if price_hash(frame) != digest:
                raise ValueError(f"audit snapshot changed: {ticker}")
            members.append(
                AuditMember(
                    ticker, digest, datetime.fromisoformat(item["source_retrieved_at"]), path
                )
            )
        if len(members) != metadata["size"] or len({m.ticker for m in members}) != len(members):
            raise ValueError("audit cohort size or uniqueness changed")
        return AuditCohort(
            date.fromisoformat(metadata["completed_session"]),
            datetime.fromisoformat(metadata["frozen_at"]),
            tuple(members),
        )
    except (OSError, KeyError, TypeError, ValueError, pl.exceptions.PolarsError) as exc:
        raise CacheIntegrityError("invalid immutable audit cohort") from exc


def load_audit_snapshot(data_dir: Path, ticker: str) -> tuple[AuditMember, pl.DataFrame]:
    cohort = read_audit_cohort(data_dir)
    if cohort is None:
        raise ValueError("audit cohort has not been frozen")
    for member in cohort.members:
        if member.ticker == ticker:
            return member, pl.read_parquet(member.snapshot)
    raise ValueError(f"{ticker} is not in the frozen audit cohort")


def freeze_audit_cohort(
    data_dir: Path,
    eligible_nasdaq_tickers: list[str],
    completed_session: date,
    *,
    size: int = AUDIT_SIZE,
    calendar: SessionCalendar | None = None,
) -> AuditCohort:
    """Select eligible verified caches by stable hash rank and freeze once."""
    existing = read_audit_cohort(data_dir)
    if existing is not None:
        return existing
    if size <= 0:
        raise ValueError("audit cohort size must be positive")
    calendar = calendar or SessionCalendar()
    expected_tail = calendar.sessions(completed_session - timedelta(days=120), completed_session)[
        -61:
    ]
    if len(expected_tail) != 61 or expected_tail[-1] != completed_session:
        raise ValueError("audit cohort needs a completed trading session")
    store = ForecastPriceStore(data_dir, YahooForecastProvider())
    verified: list[tuple[str, pl.DataFrame, datetime]] = []
    for ticker in sorted(set(eligible_nasdaq_tickers)):
        if normalize_ticker(ticker) != ticker:
            continue
        try:
            frame = store.read(ticker)
        except CacheIntegrityError:
            continue
        if frame is None or frame["ts"][-1] != completed_session:
            continue
        if tuple(frame["ts"].to_list()[-61:]) != expected_tail:
            continue
        if clean_completed(frame, completed_session, calendar).height != frame.height:
            continue
        manifest = json.loads(store.path(ticker).with_suffix(".json").read_text())
        retrieved_at = datetime.fromisoformat(manifest["retrieved_at"])
        if retrieved_at.tzinfo is None:
            continue
        verified.append((ticker, frame, retrieved_at.astimezone(UTC)))
    if len(verified) < size:
        raise ValueError(f"only {len(verified)} eligible verified caches; need {size}")
    verified.sort(key=lambda row: (hashlib.sha256(row[0].encode()).hexdigest(), row[0]))
    root = data_dir / "forecast" / "audit"
    members = []
    for ticker, frame, retrieved_at in verified[:size]:
        digest = price_hash(frame)
        path = root / "snapshots" / f"{ticker}-{digest}.parquet"
        _write_once(path, frame.write_parquet)
        if price_hash(pl.read_parquet(path)) != digest:
            raise CacheIntegrityError(f"audit snapshot changed: {ticker}")
        members.append(AuditMember(ticker, digest, retrieved_at, path))
    frozen_at = datetime.now(UTC)
    cohort = AuditCohort(completed_session, frozen_at, tuple(members))
    metadata = {
        "provenance": cohort.provenance,
        "size": size,
        "completed_session": completed_session.isoformat(),
        "frozen_at": frozen_at.isoformat(),
        "members": [
            {
                "ticker": item.ticker,
                "data_hash": item.data_hash,
                "source_retrieved_at": item.source_retrieved_at.isoformat(),
            }
            for item in members
        ],
    }
    _write_once(
        root / "cohort.json", lambda path: path.write_text(json.dumps(metadata, sort_keys=True))
    )
    return read_audit_cohort(data_dir) or cohort
