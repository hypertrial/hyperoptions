"""Immutable, hash-verified local inputs for retrospective model training.

These are frozen from an existing verified cache, never fetched here. They are
current-vintage replay inputs, not evidence of what was available at an old origin.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from options_api.models import normalize_ticker
from stocksweeper.forecast.audit import AuditCohort, _write_once, read_audit_cohort
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import (
    CacheIntegrityError,
    ForecastPriceStore,
    PRICE_COLUMNS,
    YahooForecastProvider,
    clean_completed,
    price_hash,
)

TRAINING_SIZE = 100
TRAINING_BARS = 756
MIN_TRAINING_BARS = 500
_PROVENANCE = "immutable_current_vintage_training"
_UNQUALIFIED_RIGHTS = "training_rights_unverified"
_APPROVED_RIGHTS = "approved_for_training"


class SourceRightsUnverified(ValueError):
    """A valid local snapshot does not grant permission to train on its source."""


def _has_approval(metadata: dict[str, object]) -> bool:
    source = metadata.get("source_name")
    reference = metadata.get("license_reference")
    return (
        isinstance(source, str)
        and bool(source.strip())
        and isinstance(reference, str)
        and bool(reference.strip())
        and metadata.get("source") == source
    )


def _vintage_paths(data_dir: Path, ticker: str, session: date, digest: str) -> tuple[Path, Path]:
    if (
        normalize_ticker(ticker) != ticker
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("invalid price vintage identity")
    root = data_dir / "forecast" / "vintages" / ticker / session.isoformat()
    return root / f"{digest}.parquet", root / f"{digest}.json"


def read_price_vintage(
    data_dir: Path, ticker: str, session: date, digest: str
) -> tuple[pl.DataFrame, dict[str, str]]:
    path, manifest = _vintage_paths(data_dir, ticker, session, digest)
    try:
        metadata = json.loads(manifest.read_text())
        frame = pl.read_parquet(path)
        expected_schema = {name: pl.Date if name == "ts" else pl.Float64 for name in PRICE_COLUMNS}
        rights = metadata.get("rights_status") if isinstance(metadata, dict) else None
        manifest_digest = metadata.get("manifest_hash") if isinstance(metadata, dict) else None
        signed_metadata = (
            {key: value for key, value in metadata.items() if key != "manifest_hash"}
            if isinstance(metadata, dict)
            else {}
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("ticker") != ticker
            or metadata.get("through_session") != session.isoformat()
            or metadata.get("data_hash") != digest
            or metadata.get("provenance") != _PROVENANCE
            or rights not in (_UNQUALIFIED_RIGHTS, _APPROVED_RIGHTS)
            or (rights == _APPROVED_RIGHTS and not _has_approval(metadata))
            or manifest_digest != _manifest_hash(signed_metadata)
            or metadata.get("price_basis") != "split-normalized, dividend-unadjusted"
            or frame.schema != expected_schema
            or frame.is_empty()
            or frame["ts"][-1] != session
            or tuple(frame["ts"]) != SessionCalendar().sessions(frame["ts"][0], session)
            or clean_completed(frame, session, SessionCalendar()).height != frame.height
            or price_hash(frame) != digest
            or datetime.fromisoformat(metadata["source_retrieved_at"]).tzinfo is None
            or datetime.fromisoformat(metadata["frozen_at"]).tzinfo is None
        ):
            raise ValueError("vintage manifest does not match prices")
    except (OSError, KeyError, TypeError, ValueError, pl.exceptions.PolarsError) as exc:
        raise CacheIntegrityError(f"invalid immutable price vintage for {ticker}") from exc
    return frame, metadata


def freeze_price_vintage(
    data_dir: Path, ticker: str, completed_session: date
) -> tuple[pl.DataFrame, str]:
    """Copy up to three years from an already-local, verified completed cache."""
    store = ForecastPriceStore(data_dir, YahooForecastProvider())
    source = store.read(ticker)
    if source is None or source["ts"][-1] != completed_session:
        raise ValueError("verified latest completed price bar is unavailable")
    frame = source.tail(TRAINING_BARS)
    calendar = SessionCalendar()
    if (
        frame.height < MIN_TRAINING_BARS
        or tuple(frame["ts"]) != calendar.sessions(frame["ts"][0], completed_session)
        or clean_completed(frame, completed_session, calendar).height != frame.height
        or any(value > 0 for value in frame["stock_splits"])
    ):
        raise ValueError("contiguous split-safe training history is unavailable")
    try:
        source_metadata = json.loads(store.path(ticker).with_suffix(".json").read_text())
        if (
            not isinstance(source_metadata, dict)
            or source_metadata.get("hash") != price_hash(source)
            or source_metadata.get("through_session") != completed_session.isoformat()
        ):
            raise ValueError("price cache changed while freezing vintage")
        retrieved_at = datetime.fromisoformat(source_metadata["retrieved_at"])
        if retrieved_at.tzinfo is None:
            raise ValueError("source retrieval time is not timezone-aware")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise CacheIntegrityError("price cache changed while freezing vintage") from exc
    digest = price_hash(frame)
    path, manifest = _vintage_paths(data_dir, ticker, completed_session, digest)
    _write_once(path, frame.write_parquet)
    metadata = {
        "ticker": ticker,
        "through_session": completed_session.isoformat(),
        "data_hash": digest,
        "source_hash": source_metadata["hash"],
        "source": source_metadata["source"],
        "source_retrieved_at": retrieved_at.astimezone(UTC).isoformat(),
        "frozen_at": datetime.now(UTC).isoformat(),
        "price_basis": source_metadata["price_basis"],
        "provenance": _PROVENANCE,
        "rights_status": _UNQUALIFIED_RIGHTS,
    }
    metadata["manifest_hash"] = _manifest_hash(metadata)
    _write_once(
        manifest, lambda temporary: temporary.write_text(json.dumps(metadata, sort_keys=True))
    )
    verified, _ = read_price_vintage(data_dir, ticker, completed_session, digest)
    return verified, digest


def _cohort_manifest(data_dir: Path) -> Path:
    return data_dir / "forecast" / "training" / "cohort.json"


def _manifest_hash(metadata: dict[str, object]) -> str:
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _audit_identity(audit: AuditCohort) -> str:
    return _manifest_hash(
        {"members": sorted((member.ticker, member.data_hash) for member in audit.members)}
    )


def load_training_cohort(
    data_dir: Path, *, require_training_rights: bool = True
) -> tuple[tuple[str, pl.DataFrame, str], ...]:
    """Load a frozen cohort, rejecting changed members and changed manifest."""
    try:
        metadata = json.loads(_cohort_manifest(data_dir).read_text())
        digest = metadata.pop("manifest_hash")
        if (
            metadata.get("provenance") != _PROVENANCE
            or metadata.get("rights_status") not in (_UNQUALIFIED_RIGHTS, _APPROVED_RIGHTS)
            or _manifest_hash(metadata) != digest
            or len(metadata["members"]) != metadata["size"]
        ):
            raise ValueError("invalid training cohort manifest")
        rights = metadata["rights_status"]
        if rights == _APPROVED_RIGHTS and not _has_approval(metadata):
            raise ValueError("approved source and license reference are required")
        session = date.fromisoformat(metadata["completed_session"])
        audit = read_audit_cohort(data_dir)
        if audit is None or metadata.get("audit_cohort_hash") != _audit_identity(audit):
            raise ValueError("separate audit cohort changed or disappeared")
        excluded = {member.ticker for member in audit.members}
        result = []
        for member in metadata["members"]:
            ticker, member_hash = member["ticker"], member["data_hash"]
            if ticker in excluded:
                raise ValueError("training cohort overlaps audit cohort")
            frame, vintage = read_price_vintage(data_dir, ticker, session, member_hash)
            if vintage["rights_status"] != rights or (
                rights == _APPROVED_RIGHTS
                and (
                    vintage["source_name"] != metadata["source_name"]
                    or vintage["license_reference"] != metadata["license_reference"]
                )
            ):
                raise ValueError("training member source rights differ from cohort")
            result.append((ticker, frame, member_hash))
        if len({ticker for ticker, _, _ in result}) != len(result):
            raise ValueError("duplicate training cohort ticker")
        if require_training_rights and rights != _APPROVED_RIGHTS:
            raise SourceRightsUnverified(_UNQUALIFIED_RIGHTS)
        return tuple(result)
    except SourceRightsUnverified:
        raise
    except (OSError, KeyError, TypeError, ValueError, CacheIntegrityError) as exc:
        raise CacheIntegrityError("invalid immutable training cohort") from exc


def freeze_training_cohort(
    data_dir: Path,
    eligible_tickers: Iterable[str],
    completed_session: date,
    *,
    size: int = TRAINING_SIZE,
    exclude_tickers: Iterable[str] = (),
) -> tuple[tuple[str, pl.DataFrame, str], ...]:
    """Deterministically freeze only verified local Nasdaq-member caches."""
    manifest = _cohort_manifest(data_dir)
    if manifest.exists():
        existing = load_training_cohort(data_dir, require_training_rights=False)
        if len(existing) != size or any(
            frame["ts"][-1] != completed_session for _, frame, _ in existing
        ):
            raise ValueError("existing frozen cohort has another size or session")
        return existing
    if size <= 0:
        raise ValueError("training cohort size must be positive")
    audit = read_audit_cohort(data_dir)
    if audit is None:
        raise ValueError("freeze the separate audit cohort first")
    excluded = set(exclude_tickers) | {member.ticker for member in audit.members}
    eligible = sorted(set(eligible_tickers) - excluded)
    ranked = sorted(
        (ticker for ticker in eligible if normalize_ticker(ticker) == ticker),
        key=lambda ticker: (hashlib.sha256(ticker.encode()).hexdigest(), ticker),
    )
    members = []
    for ticker in ranked:
        try:
            _, digest = freeze_price_vintage(data_dir, ticker, completed_session)
        except (CacheIntegrityError, OSError, ValueError):
            continue
        members.append({"ticker": ticker, "data_hash": digest})
        if len(members) == size:
            break
    if len(members) != size:
        raise ValueError(f"only {len(members)} verified disjoint training caches; need {size}")
    metadata: dict[str, object] = {
        "provenance": _PROVENANCE,
        "rights_status": _UNQUALIFIED_RIGHTS,
        "audit_cohort_hash": _audit_identity(audit),
        "completed_session": completed_session.isoformat(),
        "size": size,
        "members": members,
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    metadata["manifest_hash"] = _manifest_hash(metadata)
    _write_once(
        manifest, lambda temporary: temporary.write_text(json.dumps(metadata, sort_keys=True))
    )
    return load_training_cohort(data_dir, require_training_rights=False)
