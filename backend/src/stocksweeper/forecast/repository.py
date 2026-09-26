"""Durable watchlist forecast versions and evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from stocksweeper.forecast.models import ForecastSnapshot
from stocksweeper.storage.db import connect, rows

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_selections (
    ticker VARCHAR NOT NULL,
    prefix_hash VARCHAR NOT NULL,
    catalog_hash VARCHAR NOT NULL,
    payload_json VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (ticker, prefix_hash, catalog_hash)
);

CREATE TABLE IF NOT EXISTS forecast_cohorts (
    singleton VARCHAR PRIMARY KEY,
    candidate_hash VARCHAR NOT NULL,
    catalog_hash VARCHAR NOT NULL,
    payload_json VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_cohort_versions (
    version_id VARCHAR PRIMARY KEY,
    payload_json VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_cohort_attempts (
    month VARCHAR NOT NULL,
    candidate_hash VARCHAR NOT NULL,
    catalog_hash VARCHAR NOT NULL,
    payload_json VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (month, candidate_hash, catalog_hash)
);

CREATE TABLE IF NOT EXISTS forecast_model_versions (
    model_id VARCHAR PRIMARY KEY,
    month VARCHAR NOT NULL,
    horizon INTEGER NOT NULL,
    candidate_hash VARCHAR NOT NULL,
    catalog_hash VARCHAR NOT NULL,
    data_hash VARCHAR,
    qualified BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_evidence (
    model_id VARCHAR PRIMARY KEY,
    payload_json VARCHAR NOT NULL
);

CREATE INDEX IF NOT EXISTS forecast_models_by_horizon
    ON forecast_model_versions (horizon, qualified, created_at);

CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id VARCHAR PRIMARY KEY,
    content_hash VARCHAR NOT NULL UNIQUE,
    ticker VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    strike DECIMAL(18, 3) NOT NULL,
    expiry DATE NOT NULL,
    as_of DATE NOT NULL,
    model_id VARCHAR,
    data_hash VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    payload_json VARCHAR NOT NULL
);

CREATE INDEX IF NOT EXISTS forecast_snapshots_by_contract
    ON forecast_snapshots (ticker, side, strike, expiry, as_of, created_at);
"""


def _encode(payload: dict[str, Any]) -> str:
    """Require portable evidence that can be read after a process restart."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode(value: str) -> dict[str, Any]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("forecast evidence must be a JSON object")
    return payload


def _cohort_version_id(payload: dict[str, Any]) -> str:
    stable = {key: value for key, value in payload.items() if key != "version_id"}
    return hashlib.sha256(_encode(stable).encode()).hexdigest()


class ForecastRepository:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "results.duckdb"

    def initialize(self) -> None:
        with connect(self.path) as connection:
            connection.execute(_SCHEMA)

    def get_selection(
        self, ticker: str, prefix_hash: str, catalog_hash: str
    ) -> dict[str, Any] | None:
        return self._get_payload(
            "forecast_selections",
            "ticker = ? AND prefix_hash = ? AND catalog_hash = ?",
            [ticker, prefix_hash, catalog_hash],
        )

    def save_selection(
        self, ticker: str, prefix_hash: str, catalog_hash: str, payload: dict[str, Any]
    ) -> None:
        encoded = _encode(payload)
        with connect(self.path) as connection:
            connection.execute(
                """INSERT INTO forecast_selections VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                [ticker, prefix_hash, catalog_hash, encoded, datetime.now(UTC)],
            )

    def get_cohort(self) -> dict[str, Any] | None:
        with connect(self.path) as connection:
            versions = rows(
                connection,
                """SELECT version_id, payload_json FROM forecast_cohort_versions
                   ORDER BY created_at DESC, version_id DESC LIMIT 1""",
            )
        if versions:
            return {**_decode(versions[0]["payload_json"]), "version_id": versions[0]["version_id"]}
        original = self._get_payload("forecast_cohorts", "singleton = 'frozen'", [])
        return {**original, "version_id": _cohort_version_id(original)} if original else None

    def save_cohort(self, candidate_hash: str, catalog_hash: str, payload: dict[str, Any]) -> None:
        encoded = _encode(payload)
        with connect(self.path) as connection:
            connection.execute(
                """INSERT INTO forecast_cohorts VALUES ('frozen', ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                [candidate_hash, catalog_hash, encoded, datetime.now(UTC)],
            )

    def save_cohort_version(self, payload: dict[str, Any]) -> str:
        """Version selections while preserving the original cohort membership."""
        original = self._get_payload("forecast_cohorts", "singleton = 'frozen'", [])
        if original is None:
            raise ValueError("freeze a cohort before writing a new version")
        if payload.get("candidate_hash") != original.get("candidate_hash"):
            raise ValueError("cohort candidate identity cannot change")
        original_members = [
            (item["ticker"], item["sector"]) for item in original.get("members", [])
        ]
        updated_members = [(item["ticker"], item["sector"]) for item in payload.get("members", [])]
        if original_members != updated_members:
            raise ValueError("frozen cohort members and sectors cannot change")
        version_payload = {key: value for key, value in payload.items() if key != "version_id"}
        encoded = _encode(version_payload)
        version_id = _cohort_version_id(version_payload)
        with connect(self.path) as connection:
            connection.execute(
                """INSERT INTO forecast_cohort_versions VALUES (?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                [version_id, encoded, datetime.now(UTC)],
            )
        return version_id

    def get_cohort_attempt(
        self, month: str, candidate_hash: str, catalog_hash: str
    ) -> dict[str, Any] | None:
        return self._get_payload(
            "forecast_cohort_attempts",
            "month = ? AND candidate_hash = ? AND catalog_hash = ?",
            [month, candidate_hash, catalog_hash],
        )

    def save_cohort_attempt(
        self, month: str, candidate_hash: str, catalog_hash: str, payload: dict[str, Any]
    ) -> None:
        encoded = _encode(payload)
        with connect(self.path) as connection:
            connection.execute(
                """INSERT INTO forecast_cohort_attempts VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (month, candidate_hash, catalog_hash)
                   DO UPDATE SET payload_json = excluded.payload_json,
                                 created_at = excluded.created_at""",
                [month, candidate_hash, catalog_hash, encoded, datetime.now(UTC)],
            )

    def get_build(
        self, month: str, horizon: int, candidate_hash: str, catalog_hash: str
    ) -> dict[str, Any] | None:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """SELECT e.payload_json FROM forecast_model_versions AS m
                   JOIN forecast_evidence AS e USING (model_id)
                   WHERE m.month = ? AND m.horizon = ?
                     AND m.candidate_hash = ? AND m.catalog_hash = ?
                   ORDER BY m.created_at DESC, m.model_id DESC LIMIT 1""",
                [month, horizon, candidate_hash, catalog_hash],
            )
        return _decode(found[0]["payload_json"]) if found else None

    def save_build(
        self,
        month: str,
        horizon: int,
        candidate_hash: str,
        catalog_hash: str,
        payload: dict[str, Any],
    ) -> str:
        if horizon < 1:
            raise ValueError("forecast horizon must be positive")
        encoded = _encode(payload)
        supplied = payload.get("model_id")
        if supplied is not None and (not isinstance(supplied, str) or not supplied):
            raise ValueError("model_id must be a nonempty string")
        model_id = (
            supplied
            or hashlib.sha256(
                f"{month}:{horizon}:{candidate_hash}:{catalog_hash}:{encoded}".encode()
            ).hexdigest()
        )
        data_hash = payload.get("data_hash")
        if data_hash is not None and not isinstance(data_hash, str):
            raise ValueError("data_hash must be a string")
        with connect(self.path) as connection:
            existing = rows(
                connection,
                "SELECT payload_json FROM forecast_evidence WHERE model_id = ?",
                [model_id],
            )
            if existing:
                if existing[0]["payload_json"] != encoded:
                    raise ValueError("model_id already refers to different evidence")
                return model_id
            connection.begin()
            try:
                connection.execute(
                    """INSERT INTO forecast_model_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        model_id,
                        month,
                        horizon,
                        candidate_hash,
                        catalog_hash,
                        data_hash,
                        payload.get("qualified") is True,
                        datetime.now(UTC),
                    ],
                )
                connection.execute(
                    "INSERT INTO forecast_evidence VALUES (?, ?)", [model_id, encoded]
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return model_id

    def latest_qualified(self, horizon: int) -> dict[str, Any] | None:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """SELECT e.payload_json FROM forecast_model_versions AS m
                   JOIN forecast_evidence AS e USING (model_id)
                   WHERE m.horizon = ? AND m.qualified
                   ORDER BY m.created_at DESC, m.model_id DESC LIMIT 1""",
                [horizon],
            )
        return _decode(found[0]["payload_json"]) if found else None

    def save_snapshot(self, snapshot: ForecastSnapshot) -> None:
        payload = snapshot.model_dump(mode="json")
        encoded = _encode(payload)
        stable_payload = {key: value for key, value in payload.items() if key != "created_at"}
        content_hash = hashlib.sha256(_encode(stable_payload).encode()).hexdigest()
        with connect(self.path) as connection:
            connection.execute(
                """INSERT INTO forecast_snapshots
                   (id, content_hash, ticker, side, strike, expiry, as_of, model_id,
                    data_hash, created_at, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                [
                    uuid.uuid4().hex,
                    content_hash,
                    snapshot.ticker,
                    snapshot.side,
                    snapshot.strike,
                    snapshot.expiry,
                    snapshot.as_of,
                    snapshot.model_id,
                    snapshot.data_hash,
                    snapshot.created_at,
                    encoded,
                ],
            )

    def latest_snapshot(
        self,
        ticker: str,
        side: Literal["call", "put"],
        strike: Decimal,
        expiry: date,
    ) -> ForecastSnapshot | None:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """SELECT payload_json FROM forecast_snapshots
                   WHERE ticker = ? AND side = ? AND strike = ? AND expiry = ?
                   ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""",
                [ticker, side, strike, expiry],
            )
        return ForecastSnapshot.model_validate(_decode(found[0]["payload_json"])) if found else None

    def last_available_snapshot(
        self,
        ticker: str,
        side: Literal["call", "put"],
        strike: Decimal,
        expiry: date,
    ) -> ForecastSnapshot | None:
        """Retain historical numerical evidence when a later model is unavailable."""
        with connect(self.path) as connection:
            found = rows(
                connection,
                """SELECT payload_json FROM forecast_snapshots
                   WHERE ticker = ? AND side = ? AND strike = ? AND expiry = ?
                     AND as_of < ?
                     AND json_extract_string(payload_json, '$.status') = 'available'
                   ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""",
                [ticker, side, strike, expiry, expiry],
            )
        return ForecastSnapshot.model_validate(_decode(found[0]["payload_json"])) if found else None

    def _get_payload(
        self,
        table: Literal["forecast_selections", "forecast_cohorts", "forecast_cohort_attempts"],
        predicate: str,
        params: list[object],
    ) -> dict[str, Any] | None:
        with connect(self.path) as connection:
            found = rows(connection, f"SELECT payload_json FROM {table} WHERE {predicate}", params)
        return _decode(found[0]["payload_json"]) if found else None
