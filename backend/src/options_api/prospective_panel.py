"""Daily prospective forecast panel from a frozen, explicitly limited audit cohort.

Each tick handles at most one ticker. The panel selects actual listed contracts
from a fresh chain and records empty sampling cells, so missing coverage is
visible alongside as-issued forecasts. It is a convenience sample, not a
claim about every Nasdaq listing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from options_api.live_quant import quant_for_contract
from options_api.market_calendar import (
    first_session_after_completed,
    latest_completed_session,
    regular_session_open,
)
from options_api.market_watch import MarketWatchOdds
from options_api.models import OptionChainResponse, OptionQuote, Side
from options_api.outcomes import TERMS_NOTE
from options_api.physical_shadow_capture import PhysicalShadowCapture
from options_api.predictive_watch import PredictiveWatchOdds
from options_api.service import OptionChainService
from stocksweeper.forecast.audit import AUDIT_SIZE, AuditCohort, read_audit_cohort
from stocksweeper.forecast.calibration import moneyness_band
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.ledger import ForecastIssuance
from stocksweeper.forecast.predictive import PredictiveDistribution
from stocksweeper.storage.db import connect, rows

LOG = logging.getLogger(__name__)
_NY = ZoneInfo("America/New_York")
_MIN_INTERVAL_SECONDS = 30
_MAX_CHAIN_AGE = timedelta(minutes=2)
_BANDS = (("1", 1, 1, 1), ("2-5", 2, 5, 3), ("6-25", 6, 25, 15))
_MONEYNESS = (
    ("near ATM", Decimal("0")),
    ("moderately ITM", Decimal("0.10")),
    ("moderately OTM", Decimal("0.10")),
    ("far ITM", Decimal("0.20")),
    ("far OTM", Decimal("0.20")),
)
_SIDES: tuple[Side, ...] = ("call", "put")
Entry = tuple[ForecastIssuance, PredictiveDistribution | None]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PanelCell:
    horizon_band: str
    moneyness: str
    side: Side
    row: OptionQuote | None
    reason: str | None


def _side_listed(row: OptionQuote, side: Side) -> bool:
    values = (
        (row.call_bid, row.call_ask, row.call_volume, row.call_open_interest)
        if side == "call"
        else (row.put_bid, row.put_ask, row.put_volume, row.put_open_interest)
    )
    return any(value is not None for value in values)


def _empty_cells(reason: str) -> list[PanelCell]:
    return [
        PanelCell(band, money, side, None, reason)
        for band, *_ in _BANDS
        for money, _ in _MONEYNESS
        for side in _SIDES
    ]


def select_contracts(
    chain: OptionChainResponse, input_session: date, calendar: SessionCalendar
) -> list[PanelCell]:
    """Choose one actual contract per declared horizon, moneyness, and side cell."""
    spot = chain.spot
    if spot is None or not spot.is_finite() or spot <= 0:
        return _empty_cells("selection_spot_unavailable")
    if not chain.options_available:
        return _empty_cells("options_unavailable")
    if chain.truncated:
        return _empty_cells("chain_truncated")

    duplicate_keys: set[tuple[str, Decimal]] = set()
    seen: set[tuple[str, Decimal]] = set()
    for row in chain.rows:
        key = (row.expiration, row.strike)
        if key in seen:
            duplicate_keys.add(key)
        seen.add(key)
    eligible: dict[date, list[OptionQuote]] = {}
    horizons: dict[date, int] = {}
    for row in chain.rows:
        if (
            row.ticker != chain.ticker
            or row.root != chain.ticker
            or row.identity_reason is not None
            or (row.expiration, row.strike) in duplicate_keys
            or not row.strike.is_finite()
            or row.strike <= 0
        ):
            continue
        try:
            expiry = date.fromisoformat(row.expiration)
            if not 0 < (expiry - input_session).days <= 45:
                continue
            horizon = calendar.horizon(input_session, expiry)
        except (ValueError, OverflowError):
            continue
        if not 1 <= horizon <= 25:
            continue
        eligible.setdefault(expiry, []).append(row)
        horizons[expiry] = horizon

    if not eligible:
        return _empty_cells("no_standard_contracts")
    selected: list[PanelCell] = []
    for band, low, high, target_horizon in _BANDS:
        expiries = [expiry for expiry, horizon in horizons.items() if low <= horizon <= high]
        if not expiries:
            selected.extend(
                PanelCell(band, money, side, None, "no_expiry_in_band")
                for money, _ in _MONEYNESS
                for side in _SIDES
            )
            continue
        expiry = min(
            expiries,
            key=lambda item: (abs(horizons[item] - target_horizon), horizons[item], item),
        )
        for money, target_distance in _MONEYNESS:
            for side in _SIDES:
                candidates = [
                    row
                    for row in eligible[expiry]
                    if _side_listed(row, side) and moneyness_band(spot, row.strike, side) == money
                ]
                if not candidates:
                    selected.append(
                        PanelCell(band, money, side, None, "no_listed_contract_in_bucket")
                    )
                    continue
                choice = min(
                    candidates,
                    key=lambda row: (abs(abs(row.strike / spot - 1) - target_distance), row.strike),
                )
                selected.append(PanelCell(band, money, side, choice, None))
    return selected


class ProspectivePanel:
    """Run one rate-limited ticker per scheduler tick; never serve live API odds."""

    def __init__(
        self,
        data_dir: Path,
        service: OptionChainService,
        market: MarketWatchOdds,
        predictive: PredictiveWatchOdds,
        shadow: PhysicalShadowCapture,
    ) -> None:
        self.data_dir = data_dir
        self.service = service
        self.market = market
        self.predictive = predictive
        self.shadow = shadow
        self._cohort: AuditCohort | None = None
        self._lock = asyncio.Lock()
        self._last_attempt = float("-inf")

    @property
    def _path(self) -> Path:
        return self.data_dir / "results.duckdb"

    @staticmethod
    def _cohort_hash(cohort: AuditCohort) -> str:
        evidence = "|".join(
            f"{member.ticker}:{member.data_hash}"
            for member in sorted(cohort.members, key=lambda m: m.ticker)
        )
        return hashlib.sha256(evidence.encode()).hexdigest()

    def _next_ticker(self, cohort: AuditCohort, sample_session: date) -> str | None:
        digest = self._cohort_hash(cohort)
        with connect(self._path) as connection:
            completed = {
                row["ticker"]
                for row in rows(
                    connection,
                    """SELECT DISTINCT ticker FROM forecast_panel_cells
                       WHERE sample_session = ? AND cohort_hash = ?""",
                    [sample_session, digest],
                )
            }
        order = sorted(
            (member.ticker for member in cohort.members if member.ticker not in completed),
            key=lambda ticker: (
                hashlib.sha256(f"{sample_session}:{ticker}".encode()).digest(),
                ticker,
            ),
        )
        return order[0] if order else None

    def _record_cells(
        self,
        cohort: AuditCohort,
        sample_session: date,
        ticker: str,
        input_session: date,
        attempted_at: datetime,
        chain: OptionChainResponse | None,
        cells: list[tuple[PanelCell, str | None, str, str | None]],
    ) -> None:
        with connect(self._path) as connection:
            connection.begin()
            try:
                for cell, contract_key, status, reason in cells:
                    connection.execute(
                        """INSERT INTO forecast_panel_cells VALUES (
                             ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT DO NOTHING""",
                        [
                            sample_session,
                            ticker,
                            self._cohort_hash(cohort),
                            cohort.frozen_at,
                            input_session,
                            attempted_at,
                            cell.horizon_band,
                            cell.moneyness,
                            cell.side,
                            chain.source if chain else None,
                            chain.fetched_at if chain else None,
                            str(chain.spot) if chain and chain.spot is not None else None,
                            contract_key,
                            status,
                            reason,
                        ],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _issue_selected(
        self,
        ticker: str,
        started: datetime,
        input_session: date,
        chain_fetched_at: datetime | None,
        selected: list[PanelCell],
    ) -> tuple[list[Entry], list[tuple[PanelCell, str | None, str, str | None]]]:
        entries: list[Entry] = []
        recorded: list[tuple[PanelCell, str | None, str, str | None]] = []
        for cell in selected:
            row = cell.row
            if row is None:
                recorded.append((cell, None, "missing", cell.reason))
                continue
            # The model lookup is CPU work. It runs off the event loop, and
            # session rollover cannot turn an expired contract into evidence.
            now = _now()
            if not regular_session_open(now) or latest_completed_session(now) != input_session:
                recorded.append((cell, None, "missing", "input_session_rolled_over"))
                continue
            if chain_fetched_at is None or now - chain_fetched_at > _MAX_CHAIN_AGE:
                recorded.append((cell, None, "missing", "chain_snapshot_stale"))
                continue
            expiry = date.fromisoformat(row.expiration)
            try:
                result = quant_for_contract(
                    self.market,
                    self.predictive,
                    ticker=ticker,
                    root=ticker,
                    side=cell.side,
                    expiry=expiry,
                    strike=row.strike,
                    contract_since=first_session_after_completed(started),
                    terms_note=TERMS_NOTE,
                )
            except Exception:
                LOG.exception("audit panel forecast failed for %s", ticker)
                recorded.append((cell, None, "unavailable", "quant_error"))
                continue
            if result.issuance is None:
                recorded.append((cell, None, "unavailable", "issuance_unavailable"))
                continue
            issue, distribution = result.issuance
            if (
                issue.input_session != input_session
                or issue.issued_at < started
                or (chain_fetched_at is not None and issue.issued_at < chain_fetched_at)
                or issue.issued_at > _now()
            ):
                recorded.append((cell, None, "unavailable", "issuance_clock_mismatch"))
                continue
            entries.append((issue, distribution))
            recorded.append(
                (
                    cell,
                    issue.contract_key,
                    "issued" if issue.status == "available" else "unavailable",
                    issue.unavailable_reason,
                )
            )
        return entries, recorded

    async def tick(self) -> str | None:
        """Sample at most one cohort ticker; return it, or None when no work is due."""
        if self._lock.locked():
            return None
        async with self._lock:
            started = _now()
            if not regular_session_open(started):
                return None
            if time.monotonic() - self._last_attempt < _MIN_INTERVAL_SECONDS:
                return None
            if self._cohort is None:
                try:
                    self._cohort = await asyncio.to_thread(read_audit_cohort, self.data_dir)
                except Exception:
                    LOG.exception("immutable audit cohort is invalid")
                    return None
            cohort = self._cohort
            if cohort is None or len(cohort.members) != AUDIT_SIZE:
                return None
            sample_session = started.astimezone(_NY).date()
            ticker = await asyncio.to_thread(self._next_ticker, cohort, sample_session)
            if ticker is None:
                return None
            self._last_attempt = time.monotonic()
            input_session = latest_completed_session(started)
            chain: OptionChainResponse | None = None
            failure: str | None = None
            # Yahoo refresh can take longer than the chain freshness window.
            # Finish it first, then fetch the actual listed contracts.
            await self.predictive._refresh(ticker)
            current = _now()
            if (
                not regular_session_open(current)
                or latest_completed_session(current) != input_session
            ):
                failure = "input_session_rolled_over"
            else:
                try:
                    chain = await self.service.get_current_chain(ticker)
                except Exception as exc:
                    kind = getattr(exc, "kind", None)
                    failure = (
                        f"chain_{kind}" if isinstance(kind, str) else "chain_source_unavailable"
                    )
                    LOG.warning("audit panel chain unavailable for %s: %s", ticker, failure)
                if chain is None and failure is None:
                    failure = "chain_source_unavailable"
            if chain is not None:
                current = _now()
                fetched = chain.fetched_at
                if fetched.tzinfo is None:
                    failure = "chain_timestamp_unverified"
                elif chain.ticker != ticker:
                    failure = "chain_identity_mismatch"
                elif chain.from_cache:
                    failure = "chain_cache_reused"
                elif (
                    fetched < started - timedelta(seconds=1)
                    or fetched > current
                    or current - fetched > _MAX_CHAIN_AGE
                ):
                    failure = "chain_snapshot_stale"
                elif chain.truncated:
                    failure = "chain_truncated"
                elif not chain.options_available:
                    failure = "options_unavailable"
            selected = (
                _empty_cells(failure)
                if failure is not None
                else select_contracts(chain, input_session, self.predictive.forecaster.calendar)
            )
            entries, recorded = await asyncio.to_thread(
                self._issue_selected,
                ticker,
                started,
                input_session,
                chain.fetched_at if chain else None,
                selected,
            )
            if entries:
                try:
                    await asyncio.to_thread(self.predictive.ledger.record_batch, entries)
                except Exception:
                    LOG.exception("audit panel forecast ledger failed for %s", ticker)
                    recorded = [
                        (cell, key, "unavailable", "ledger_unavailable")
                        if key is not None
                        else (cell, key, status, reason)
                        for cell, key, status, reason in recorded
                    ]
                else:
                    try:
                        self.shadow.submit(entries)
                    except Exception:
                        LOG.exception("audit panel shadow queue failed for %s", ticker)
                        recorded = [
                            (cell, key, status, "shadow_enqueue_failed")
                            if key is not None and status == "issued"
                            else (cell, key, status, reason)
                            for cell, key, status, reason in recorded
                        ]
            await asyncio.to_thread(
                self._record_cells,
                cohort,
                sample_session,
                ticker,
                input_session,
                started,
                chain,
                recorded,
            )
            return ticker
