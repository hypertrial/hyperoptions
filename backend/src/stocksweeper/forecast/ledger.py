"""Append-only evidence for forecasts and their exact expiry-close labels."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

import duckdb

from options_api.contract_identity import parse_watch_key
from options_api.outcomes import TERMS_NOTE, classify
from stocksweeper.forecast.audit import read_audit_cohort
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import (
    CacheIntegrityError,
    ForecastPriceStore,
    YahooForecastProvider,
    clean_completed,
    price_hash,
)
from stocksweeper.storage.db import connect, rows

Provenance = Literal["as_issued", "immutable_replay"]
IssueStatus = Literal["available", "unavailable"]
LabelStatus = Literal["valid", "pending", "excluded"]


class TerminalDistribution(Protocol):
    @property
    def prices(self) -> tuple[float, ...]: ...

    @property
    def weights(self) -> tuple[float, ...]: ...


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("ledger timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _hash(record: dict[str, object]) -> str:
    data = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode()).hexdigest()


def distribution_digest(prices: tuple[float, ...], weights: tuple[float, ...]) -> str:
    if (
        not prices
        or len(prices) != len(weights)
        or any(not math.isfinite(price) or price <= 0 for price in prices)
        or any(not math.isfinite(weight) or weight < 0 for weight in weights)
        or any(prices[index] > prices[index + 1] for index in range(len(prices) - 1))
        or abs(sum(weights) - 1.0) > 1e-8
    ):
        raise ValueError("invalid terminal-price distribution")
    return _hash({"terminal_prices": prices, "weights": weights})


@dataclass(frozen=True)
class ForecastIssuance:
    contract_key: str
    ticker: str
    root: str
    side: Literal["call", "put"]
    expiration: date
    expiry_session: date
    strike_exact: str
    terms_note: str
    contract_since: date | None
    input_session: date | None
    input_retrieved_at: datetime | None
    issued_at: datetime
    model_version: str | None
    method: str | None
    data_hash: str | None
    distribution_hash: str | None
    price_basis: str | None
    spot_exact: str | None
    status: IssueStatus
    itm_probability: float | None
    otm_probability: float | None
    atm_probability: float | None
    unavailable_reason: str | None
    provenance: Provenance = "as_issued"
    volatility_regime: str | None = None
    known_event_status: str | None = None
    quote_time: datetime | None = None
    quote_source: str | None = None
    quote_fetched_at: datetime | None = None
    quote_digest: str | None = None
    snapshot_window: Literal["10:00", "13:00", "15:30"] | None = None
    prepare_ms: float | None = None
    lookup_ms: float | None = None
    idempotency_key: str | None = None

    def checked(self) -> ForecastIssuance:
        parsed = parse_watch_key(self.contract_key)
        if parsed != (
            self.ticker,
            self.root,
            self.side,
            self.expiration.isoformat(),
            Decimal(self.strike_exact),
        ):
            raise ValueError("forecast contract identity is inconsistent")
        if self.expiry_session > self.expiration:
            raise ValueError("expiry session cannot follow contract expiry")
        if not self.terms_note or self.provenance not in ("as_issued", "immutable_replay"):
            raise ValueError("forecast terms or provenance missing")
        issued_at = _utc(self.issued_at)
        retrieved_at = _utc(self.input_retrieved_at) if self.input_retrieved_at else None
        quote_time = _utc(self.quote_time) if self.quote_time else None
        quote_fetched_at = _utc(self.quote_fetched_at) if self.quote_fetched_at else None
        if retrieved_at is not None and retrieved_at > issued_at:
            raise ValueError("input cannot be retrieved after issuance")
        if quote_time is not None and quote_time > issued_at:
            raise ValueError("quote cannot be observed after issuance")
        if (quote_time is None) != (self.quote_source is None):
            raise ValueError("quote time and source must be recorded together")
        if quote_fetched_at is not None and (
            quote_time is None or quote_fetched_at < quote_time or quote_fetched_at > issued_at
        ):
            raise ValueError("quote retrieval must follow the quote and precede issuance")
        if self.quote_digest is not None and (
            quote_fetched_at is None
            or len(self.quote_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.quote_digest)
        ):
            raise ValueError("quote digest requires a verified quote retrieval")
        if self.snapshot_window not in (None, "10:00", "13:00", "15:30"):
            raise ValueError("invalid prospective snapshot window")
        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in (self.prepare_ms, self.lookup_ms)
        ):
            raise ValueError("forecast latency must be finite and nonnegative")
        if self.status == "available":
            if self.root != self.ticker or self.terms_note != TERMS_NOTE:
                raise ValueError("available forecast requires verified standard terms")
            required = (self.itm_probability, self.otm_probability, self.atm_probability)
            if any(
                value is None or not math.isfinite(value) or not 0 <= value <= 1
                for value in required
            ):
                raise ValueError("available forecast requires finite partition probabilities")
            if abs(sum(value for value in required if value is not None) - 1.0) > 1e-8:
                raise ValueError("forecast probabilities must partition one")
            if not all(
                (
                    self.input_session,
                    retrieved_at,
                    self.model_version,
                    self.method,
                    self.data_hash,
                    self.distribution_hash,
                    self.price_basis,
                    self.spot_exact,
                )
            ):
                raise ValueError("available forecast is missing its input evidence")
            if self.unavailable_reason is not None:
                raise ValueError("available forecast cannot have unavailable reason")
        elif self.status == "unavailable":
            if (
                any(
                    value is not None
                    for value in (self.itm_probability, self.otm_probability, self.atm_probability)
                )
                or self.distribution_hash is not None
                or not self.unavailable_reason
            ):
                raise ValueError("unavailable forecast must have a reason and no probabilities")
        else:
            raise ValueError("invalid forecast status")
        stable = asdict(replace(self, quote_time=quote_time, quote_fetched_at=quote_fetched_at))
        for ephemeral in (
            "idempotency_key", "issued_at", "input_retrieved_at", "prepare_ms", "lookup_ms"
        ):
            stable.pop(ephemeral)
        key = _hash(stable)
        if self.idempotency_key is not None and self.idempotency_key != key:
            raise ValueError("forecast idempotency key does not match its inputs")
        return replace(
            self,
            issued_at=issued_at,
            input_retrieved_at=retrieved_at,
            quote_time=quote_time,
            quote_fetched_at=quote_fetched_at,
            idempotency_key=key,
        )


@dataclass(frozen=True)
class ForecastLabel:
    contract_key: str
    terms_note: str
    expiry_session: date
    checked_at: datetime
    status: LabelStatus
    reason: str | None
    source: str | None
    nasdaq_close_exact: str | None
    yahoo_close_exact: str | None
    selected_close_exact: str | None
    classification: Literal["itm", "atm", "otm"] | None
    idempotency_key: str | None = None

    def checked(self) -> ForecastLabel:
        parsed = parse_watch_key(self.contract_key)
        if parsed is None or not self.terms_note:
            raise ValueError("invalid label contract identity")
        checked_at = _utc(self.checked_at)
        if self.status == "valid":
            if (
                self.selected_close_exact is None
                or self.nasdaq_close_exact is None
                or self.yahoo_close_exact is None
                or self.classification is None
                or self.reason
                or self.source != "Nasdaq historical Close (Yahoo cross-check)"
            ):
                raise ValueError("valid label needs an exact close and classification")
            nasdaq = Decimal(self.nasdaq_close_exact)
            yahoo = Decimal(self.yahoo_close_exact)
            if Decimal(self.selected_close_exact) != nasdaq or abs(nasdaq - yahoo) > Decimal(
                "0.005"
            ):
                raise ValueError("valid label needs a coherent Nasdaq and Yahoo close")
            if self.classification != classify(
                parsed[2], Decimal(self.selected_close_exact), parsed[4]
            ):
                raise ValueError("label classification contradicts exact close")
        elif (
            self.status not in ("pending", "excluded")
            or not self.reason
            or self.classification
            or self.selected_close_exact is not None
        ):
            raise ValueError("invalid label status or missing exclusion reason")
        stable = asdict(self)
        stable.pop("idempotency_key")
        key = _hash(stable)
        if self.idempotency_key is not None and self.idempotency_key != key:
            raise ValueError("label idempotency key does not match evidence")
        return replace(self, checked_at=checked_at, idempotency_key=key)


class ForecastLedger:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "results.duckdb"

    def cache_retrieved_at(
        self, ticker: str, expected_data_hash: str, input_session: date
    ) -> datetime | None:
        """Only link a forecast to the retrieval time of its exact verified input."""
        store = ForecastPriceStore(self.data_dir, YahooForecastProvider())
        frame = store.read(ticker)
        if frame is None:
            return None
        clean = clean_completed(frame, input_session, SessionCalendar())
        if clean.is_empty() or clean["ts"][-1] != input_session:
            return None
        if price_hash(clean) != expected_data_hash:
            return None
        manifest = store.path(ticker).with_suffix(".json")
        metadata = json.loads(manifest.read_text())
        return _utc(datetime.fromisoformat(metadata["retrieved_at"]))

    def record_distribution(
        self, prices: tuple[float, ...], weights: tuple[float, ...], recorded_at: datetime
    ) -> str:
        digest = distribution_digest(prices, weights)
        timestamp = _utc(recorded_at)
        with connect(self.path) as connection:
            connection.execute(
                """INSERT INTO forecast_distributions VALUES (?, ?, ?, ?)
                   ON CONFLICT (distribution_hash) DO NOTHING""",
                [digest, list(prices), list(weights), timestamp],
            )
        return digest

    def record(self, issuance: ForecastIssuance) -> bool:
        return self.record_batch([(issuance, None)]) == 1

    @staticmethod
    def _insert_issue(connection: duckdb.DuckDBPyConnection, issue: ForecastIssuance) -> None:
        assert issue.idempotency_key is not None
        connection.execute(
            """INSERT INTO forecast_issuances (
               idempotency_key, contract_key, ticker, root, side, expiration,
               expiry_session, strike_exact, terms_note, contract_since,
               input_session, input_retrieved_at, issued_at, model_version,
               method, data_hash, distribution_hash, price_basis, spot_exact,
               status, itm_probability, otm_probability, atm_probability,
               unavailable_reason, provenance, volatility_regime, known_event_status,
               quote_time, quote_source, quote_fetched_at, quote_digest, snapshot_window,
               prepare_ms, lookup_ms
               ) VALUES (
               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                issue.idempotency_key,
                issue.contract_key,
                issue.ticker,
                issue.root,
                issue.side,
                issue.expiration,
                issue.expiry_session,
                issue.strike_exact,
                issue.terms_note,
                issue.contract_since,
                issue.input_session,
                issue.input_retrieved_at,
                issue.issued_at,
                issue.model_version,
                issue.method,
                issue.data_hash,
                issue.distribution_hash,
                issue.price_basis,
                issue.spot_exact,
                issue.status,
                issue.itm_probability,
                issue.otm_probability,
                issue.atm_probability,
                issue.unavailable_reason,
                issue.provenance,
                issue.volatility_regime,
                issue.known_event_status,
                issue.quote_time,
                issue.quote_source,
                issue.quote_fetched_at,
                issue.quote_digest,
                issue.snapshot_window,
                issue.prepare_ms,
                issue.lookup_ms,
            ],
        )

    def record_batch(
        self, entries: Iterable[tuple[ForecastIssuance, TerminalDistribution | None]]
    ) -> int:
        """Record a chain's attempts and deduplicated scenarios in one transaction."""
        snapshots: dict[str, tuple[tuple[float, ...], tuple[float, ...], datetime]] = {}
        seen_distributions: dict[int, tuple[TerminalDistribution, str]] = {}
        issues: list[ForecastIssuance] = []
        for issuance, distribution in entries:
            if issuance.status == "unavailable" and distribution is not None:
                raise ValueError("unavailable forecast cannot include a distribution")
            if distribution is not None:
                previous = seen_distributions.get(id(distribution))
                digest = previous[1] if previous and previous[0] is distribution else None
                if digest is None:
                    prices, weights = tuple(distribution.prices), tuple(distribution.weights)
                    digest = distribution_digest(prices, weights)
                    seen_distributions[id(distribution)] = (distribution, digest)
                    snapshots[digest] = (prices, weights, _utc(issuance.issued_at))
                if issuance.distribution_hash is not None and issuance.distribution_hash != digest:
                    raise ValueError("distribution hash does not match issuance scenarios")
                issuance = replace(issuance, distribution_hash=digest)
            issues.append(issuance.checked())
        if not issues:
            return 0
        inserted = 0
        with connect(self.path) as connection:
            connection.begin()
            try:
                known: set[str] = set()
                for issue in issues:
                    assert issue.idempotency_key is not None
                    if (
                        issue.status == "available"
                        and issue.snapshot_window
                        and rows(
                            connection,
                            """SELECT 1 FROM forecast_issuances
                           WHERE contract_key = ? AND input_session = ?
                             AND snapshot_window = ? AND model_version = ?
                             AND status = 'available' LIMIT 1""",
                            [
                                issue.contract_key,
                                issue.input_session,
                                issue.snapshot_window,
                                issue.model_version,
                            ],
                        )
                    ):
                        continue
                    if rows(
                        connection,
                        "SELECT 1 FROM forecast_issuances WHERE idempotency_key = ?",
                        [issue.idempotency_key],
                    ):
                        continue
                    if issue.distribution_hash and issue.distribution_hash not in known:
                        snapshot = snapshots.get(issue.distribution_hash)
                        if snapshot is not None:
                            prices, weights, timestamp = snapshot
                            connection.execute(
                                """INSERT INTO forecast_distributions VALUES (?, ?, ?, ?)
                                   ON CONFLICT (distribution_hash) DO NOTHING""",
                                [issue.distribution_hash, list(prices), list(weights), timestamp],
                            )
                        elif not rows(
                            connection,
                            "SELECT 1 FROM forecast_distributions WHERE distribution_hash = ?",
                            [issue.distribution_hash],
                        ):
                            raise ValueError("forecast distribution was not recorded")
                        known.add(issue.distribution_hash)
                    self._insert_issue(connection, issue)
                    inserted += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return inserted

    def due_label_contracts(self, as_of: datetime, *, limit: int = 500) -> list[dict[str, object]]:
        """Recheck matured labels after an hour on failure, daily after success."""
        from options_api.market_calendar import latest_completed_session

        now = _utc(as_of)
        if limit < 1 or limit > 500:
            raise ValueError("label batch limit must be between 1 and 500")
        with connect(self.path) as connection:
            return rows(
                connection,
                """WITH contracts AS (
                     SELECT contract_key, terms_note, ticker, root, side, expiration,
                            expiry_session, strike_exact,
                            CASE WHEN count(contract_since) = count(*)
                                 THEN min(contract_since) ELSE NULL END AS contract_since
                     FROM forecast_issuances
                     WHERE provenance = 'as_issued' AND expiry_session <= ?
                     GROUP BY contract_key, terms_note, ticker, root, side, expiration,
                              expiry_session, strike_exact
                   ), latest AS (
                     SELECT contract_key, terms_note, expiry_session, status, checked_at,
                            row_number() OVER (
                              PARTITION BY contract_key, terms_note, expiry_session
                              ORDER BY checked_at DESC, idempotency_key DESC
                            ) AS revision_rank
                     FROM forecast_labels
                   )
                   SELECT c.* FROM contracts c LEFT JOIN latest l
                     ON c.contract_key = l.contract_key
                    AND c.terms_note = l.terms_note
                    AND c.expiry_session = l.expiry_session
                    AND l.revision_rank = 1
                   WHERE l.checked_at IS NULL
                      OR (l.status = 'valid' AND l.checked_at <= ?)
                      OR (l.status <> 'valid' AND l.checked_at <= ?)
                   ORDER BY CASE
                     WHEN l.checked_at IS NULL
                       THEN CAST(c.expiry_session AS TIMESTAMP) AT TIME ZONE 'UTC'
                     WHEN l.status = 'valid' THEN l.checked_at + INTERVAL '1 day'
                     ELSE l.checked_at + INTERVAL '1 hour'
                   END, c.expiry_session, c.ticker, c.contract_key, c.terms_note
                   LIMIT ?""",
                [
                    latest_completed_session(now),
                    now - timedelta(days=1),
                    now - timedelta(hours=1),
                    limit,
                ],
            )

    def record_label(self, label: ForecastLabel) -> bool:
        checked = label.checked()
        assert checked.idempotency_key is not None
        with connect(self.path) as connection:
            if rows(
                connection,
                "SELECT 1 FROM forecast_labels WHERE idempotency_key = ?",
                [checked.idempotency_key],
            ):
                return False
            connection.execute(
                """INSERT INTO forecast_labels (
                   idempotency_key, contract_key, terms_note, expiry_session,
                   checked_at, status, reason, source, nasdaq_close_exact,
                   yahoo_close_exact, selected_close_exact, classification
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    checked.idempotency_key,
                    checked.contract_key,
                    checked.terms_note,
                    checked.expiry_session,
                    checked.checked_at,
                    checked.status,
                    checked.reason,
                    checked.source,
                    checked.nasdaq_close_exact,
                    checked.yahoo_close_exact,
                    checked.selected_close_exact,
                    checked.classification,
                ],
            )
        return True

    def evaluation_rows(
        self, *, provenance: Provenance = "as_issued", since: date | None = None
    ) -> list[dict[str, object]]:
        """Scalar attempts on the same cohort, including unavailable and unlabelled."""
        return list(self.iter_evaluation_rows(provenance=provenance, since=since))

    def iter_evaluation_rows(
        self,
        *,
        provenance: Provenance = "as_issued",
        since: date | None = None,
        expiry_before: date | None = None,
        issued_before: datetime | None = None,
        methods: tuple[str, ...] | None = None,
        horizon_range: tuple[int, int] | None = None,
        with_crps: bool = False,
        label_as_of: datetime | None = None,
        page_size: int = 512,
    ) -> Iterator[dict[str, object]]:
        """Page scalar issuances; release the database lock between pages."""
        if not 1 <= page_size <= 4096:
            raise ValueError("evaluation page size must be between 1 and 4096")
        if methods is not None and not methods:
            return
        if horizon_range is not None and not (1 <= horizon_range[0] <= horizon_range[1]):
            raise ValueError("evaluation horizon range must be positive and ordered")
        if issued_before is not None:
            issued_before = _utc(issued_before)
        if label_as_of is not None:
            label_as_of = _utc(label_as_of)
        from options_api.market_calendar import session_close

        cursor = ""
        scores: dict[tuple[str, str], float | None] = {}
        calendar = SessionCalendar() if horizon_range is not None else None
        while True:
            filters = ["provenance = ?", "idempotency_key > ?"]
            params: list[object] = [provenance, cursor]
            if since is not None:
                filters.append("input_session >= ?")
                params.append(since)
            if expiry_before is not None:
                filters.append("expiry_session < ?")
                params.append(expiry_before)
            if issued_before is not None:
                filters.append("issued_at <= ?")
                params.append(issued_before)
            if methods is not None:
                filters.append(f"method IN ({','.join('?' for _ in methods)})")
                params.extend(methods)
            params.append(page_size)
            label_filter = "AND l.checked_at <= ?" if label_as_of is not None else ""
            if label_as_of is not None:
                params.append(label_as_of)
            query = f"""WITH issues AS MATERIALIZED (
                     SELECT * FROM forecast_issuances
                     WHERE {' AND '.join(filters)}
                     ORDER BY idempotency_key LIMIT ?
                   )
                   SELECT i.idempotency_key, i.contract_key, i.ticker, i.root, i.side,
                          i.expiration, i.expiry_session, i.strike_exact,
                          i.terms_note, i.contract_since,
                          i.input_session, i.input_retrieved_at, i.issued_at,
                          i.model_version, i.method, i.data_hash, i.price_basis,
                          i.spot_exact, i.distribution_hash,
                          i.itm_probability, i.otm_probability, i.atm_probability,
                          i.status, i.unavailable_reason, i.provenance,
                          i.volatility_regime, i.known_event_status,
                          i.quote_time, i.quote_source, i.quote_fetched_at,
                          i.quote_digest, i.snapshot_window,
                          i.prepare_ms, i.lookup_ms,
                          l.checked_at AS label_checked_at, l.status AS label_status,
                          l.reason AS label_reason, l.selected_close_exact,
                          l.classification,
                          CASE WHEN l.status = 'valid' THEN l.classification = 'itm'
                               ELSE NULL END AS observed_itm
                   FROM issues i LEFT JOIN LATERAL (
                     SELECT checked_at, status, reason, selected_close_exact,
                            classification
                     FROM forecast_labels l
                     WHERE l.contract_key = i.contract_key
                       AND l.terms_note = i.terms_note
                       AND l.expiry_session = i.expiry_session
                       {label_filter}
                     ORDER BY checked_at DESC, idempotency_key DESC LIMIT 1
                   ) l ON true
                   ORDER BY i.idempotency_key"""
            with connect(self.path) as connection:
                page = rows(connection, query, params)
            if not page:
                return
            cursor = page[-1]["idempotency_key"]
            if horizon_range is not None:
                page = [
                    item
                    for item in page
                    if item["input_session"] is not None
                    and horizon_range[0]
                    <= calendar.horizon(item["input_session"], item["expiration"])
                    <= horizon_range[1]
                ]
            if with_crps:
                keys = {
                    (item["distribution_hash"], item["selected_close_exact"])
                    for item in page
                    if item["distribution_hash"]
                    and item["selected_close_exact"]
                    and item["label_status"] == "valid"
                    and item["label_checked_at"] > item["issued_at"]
                    and (label_as_of is None or item["label_checked_at"] <= label_as_of)
                    and item["issued_at"] < session_close(item["expiry_session"])
                }
                missing = keys - scores.keys()
                scores.update({key: None for key in missing})
                scores.update(self.crps_scores(missing))
                for item in page:
                    key = (item["distribution_hash"], item["selected_close_exact"])
                    item["crps"] = scores.get(key) if key in keys else None
            yield from page

    def crps_scores(
        self, pairs: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], float]:
        """Score each distinct distribution and exact close, loading scenarios in small batches."""
        from stocksweeper.forecast.physical_evaluation import crps

        closes_by_hash: dict[str, set[str]] = {}
        for digest, close in pairs:
            closes_by_hash.setdefault(digest, set()).add(close)
        scores: dict[tuple[str, str], float] = {}
        digests = sorted(closes_by_hash)
        if not digests:
            return scores
        for start in range(0, len(digests), 64):
            batch = digests[start : start + 64]
            placeholders = ",".join("?" for _ in batch)
            with connect(self.path) as connection:
                distributions = rows(
                    connection,
                    "SELECT distribution_hash, terminal_prices, weights "
                    f"FROM forecast_distributions WHERE distribution_hash IN ({placeholders})",
                    batch,
                )
            for row in distributions:
                digest = row["distribution_hash"]
                prices = tuple(row["terminal_prices"])
                weights = tuple(row["weights"])
                for close in closes_by_hash[digest]:
                    scores[(digest, close)] = crps(prices, weights, float(Decimal(close)))
        return scores

    def evaluation_skipped_attempts(self, provenance: Provenance) -> dict[str, int]:
        """Count malformed attempts without materializing their rows."""
        with connect(self.path) as connection:
            count = connection.execute(
                """SELECT count(*) FROM forecast_issuances
                   WHERE provenance = ? AND (input_session IS NULL OR method IS NULL)""",
                [provenance],
            ).fetchone()[0]
        return {"missing_input_session_or_method": count} if count else {}

    def scorable_rows(
        self, *, provenance: Provenance = "as_issued", since: date | None = None
    ) -> list[dict[str, object]]:
        """Newest valid exact label only; revisions never multiply outcomes."""
        return [
            row
            for row in self.evaluation_rows(provenance=provenance, since=since)
            if row["status"] == "available"
            and row["label_status"] == "valid"
            and row["label_checked_at"] > row["issued_at"]
        ]

    def panel_coverage(
        self,
        *,
        since: date | None = None,
        before: date | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        """Compare recorded cells with every due fixed-cohort trading session."""
        calendar = SessionCalendar()
        try:
            cohort = read_audit_cohort(self.data_dir)
        except CacheIntegrityError:
            return {"scope": "fixed_audit_cohort_panel_cells", "cohort_status": "invalid"}
        if cohort is None:
            return {"scope": "fixed_audit_cohort_panel_cells", "cohort_status": "not_frozen"}
        first = calendar.offset(cohort.completed_session, 1)
        start = max(first, since) if since is not None else first
        latest = calendar.last_completed(as_of or datetime.now(UTC))
        end = min(latest, before - timedelta(days=1)) if before is not None else latest
        sessions = calendar.sessions(start, end) if start <= end else []
        expected_per_band = len(sessions) * len(cohort.members) * 10
        cohort_hash = hashlib.sha256(
            "|".join(
                f"{member.ticker}:{member.data_hash}"
                for member in sorted(cohort.members, key=lambda member: member.ticker)
            ).encode()
        ).hexdigest()
        with connect(self.path) as connection:
            grouped = rows(
                connection,
                """SELECT horizon_band, moneyness, forecast_status, reason,
                          count(*) AS cells
                   FROM forecast_panel_cells
                   WHERE sample_session >= ? AND sample_session <= ?
                     AND cohort_hash = ?
                   GROUP BY horizon_band, moneyness, forecast_status, reason
                   ORDER BY horizon_band, moneyness, forecast_status, reason""",
                [start, end, cohort_hash],
            )
        bands: dict[str, dict[str, object]] = {}
        for row in grouped:
            band = bands.setdefault(
                row["horizon_band"],
                {
                    "expected_cells": expected_per_band,
                    "recorded_cells": 0,
                    "status": {},
                    "rejection_reasons": {},
                    "by_moneyness": {},
                },
            )
            band["recorded_cells"] += row["cells"]
            status = band["status"]
            status[row["forecast_status"]] = status.get(row["forecast_status"], 0) + row["cells"]
            money = band["by_moneyness"].setdefault(
                row["moneyness"], {"expected_cells": expected_per_band // 5, "recorded_cells": 0}
            )
            money["recorded_cells"] += row["cells"]
            if row["reason"]:
                reasons = band["rejection_reasons"]
                reasons[row["reason"]] = reasons.get(row["reason"], 0) + row["cells"]
        for band in ("1", "2-5", "6-25"):
            bands.setdefault(
                band,
                {
                    "expected_cells": expected_per_band,
                    "recorded_cells": 0,
                    "status": {},
                    "rejection_reasons": {},
                    "by_moneyness": {},
                },
            )
        return {
            "scope": "fixed_audit_cohort_panel_cells",
            "cohort_status": "frozen",
            "cohort_size": len(cohort.members),
            "first_due_session": start.isoformat(),
            "last_completed_session": end.isoformat() if sessions else None,
            "expected_sessions": len(sessions),
            "bands": bands,
        }

    def calibration_rows(self, *, since: date) -> list[dict[str, object]]:
        """Scalar as-issued rows for daily calibration; do not load scenario arrays."""
        with connect(self.path) as connection:
            return rows(
                connection,
                """WITH latest_label AS (
                     SELECT *, row_number() OVER (
                       PARTITION BY contract_key, terms_note, expiry_session
                       ORDER BY checked_at DESC, idempotency_key DESC) AS revision_rank
                     FROM forecast_labels
                   )
                   SELECT i.idempotency_key, i.contract_key, i.ticker, i.root,
                          i.side, i.expiration, i.expiry_session, i.strike_exact,
                          i.terms_note, i.input_session, i.input_retrieved_at,
                          i.issued_at,
                          i.model_version, i.data_hash, i.price_basis,
                          i.spot_exact, i.itm_probability, i.snapshot_window,
                          l.checked_at AS label_checked_at, l.status AS label_status,
                          l.source AS label_source, l.nasdaq_close_exact,
                          l.yahoo_close_exact, l.selected_close_exact,
                          l.classification
                   FROM forecast_issuances i JOIN latest_label l
                     ON i.contract_key = l.contract_key
                    AND i.terms_note = l.terms_note
                    AND i.expiry_session = l.expiry_session
                    AND l.revision_rank = 1
                   WHERE i.provenance = 'as_issued'
                     AND i.status = 'available'
                     AND i.input_session >= ?
                     AND l.status = 'valid'
                   ORDER BY i.ticker, i.input_session, i.expiry_session,
                            i.contract_key, i.issued_at, i.idempotency_key""",
                [since],
            )

    def coverage(self) -> list[dict[str, object]]:
        """Include failed attempts in denominator by model and input session."""
        with connect(self.path) as connection:
            return rows(
                connection,
                """SELECT model_version, input_session, status,
                        unavailable_reason, count(*) AS attempts
                        FROM forecast_issuances
                        GROUP BY model_version, input_session, status, unavailable_reason
                        ORDER BY input_session, model_version""",
            )
