"""Local contract watchlist, durable outcomes, and bounded refresh jobs."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from options_api.contract_identity import make_watch_key, parse_watch_key, strike_exact
from options_api.live_quant import quant_for_contract
from options_api.market_calendar import (
    expiry_session_completed,
    first_session_after_completed,
    latest_completed_session,
    session_on_or_before,
)
from options_api.models import (
    HypotheticalRiskView,
    MarketOddsView,
    OptionQuote,
    PredictiveOddsView,
    normalize_ticker,
)
from options_api.nasdaq import NasdaqError
from options_api.outcomes import (
    TERMS_NOTE,
    CloseProvider,
    OutcomeResult,
    YahooCloseProvider,
    resolve_outcome,
)
from stocksweeper.pipeline.jobs import Job, JobBusy, JobManager
from stocksweeper.storage.db import connect, rows

LOG = logging.getLogger(__name__)
MAX_WATCHES = 256
OUTCOME_PUBLICATION_LAG = timedelta(minutes=30)


class WatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watch_key: str = Field(min_length=1, max_length=80)


class OutcomeView(BaseModel):
    status: Literal["pending", "provisional", "unsupported"]
    classification: Literal["itm", "atm", "otm"] | None = None
    reason: str | None = None
    source: str | None = None
    session_date: date | None = None
    retrieved_at: datetime | None = None
    close_exact: str | None = None
    terms_note: str = TERMS_NOTE
    revised: bool = False


class WatchItem(BaseModel):
    id: str
    ticker: str
    root: str
    side: Literal["call", "put"]
    expiration: date
    strike_exact: str
    terms_note: str
    created_at: datetime
    market_odds: MarketOddsView = Field(default_factory=MarketOddsView)
    last_available_market_odds: MarketOddsView | None = None
    predictive_odds: PredictiveOddsView = Field(default_factory=PredictiveOddsView)
    hypothetical_risk: HypotheticalRiskView = Field(default_factory=HypotheticalRiskView)
    outcome: OutcomeView


class WatchListResponse(BaseModel):
    items: list[WatchItem]
    active_job: Job | None = None


class WatchCreateResponse(BaseModel):
    item: WatchItem
    created: bool
    job: Job | None


class WatchRefreshResponse(BaseModel):
    job: Job | None


class WatchRecord(BaseModel):
    id: str
    watch_key: str
    ticker: str
    root: str
    side: Literal["call", "put"]
    expiration: date
    strike: Decimal
    terms_note: str
    created_at: datetime
    last_attempted_session: date | None = None
    last_attempted_at: datetime | None = None
    last_error: str | None = None


class WatchStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "results.duckdb"
        with connect(self.path) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info('watches')").fetchall()
            }
            if "last_error" not in columns:
                connection.execute("ALTER TABLE watches ADD COLUMN last_error VARCHAR")
            if "last_attempted_at" not in columns:
                connection.execute("ALTER TABLE watches ADD COLUMN last_attempted_at TIMESTAMPTZ")
            if "outcome_warning" not in columns:
                connection.execute("ALTER TABLE watches ADD COLUMN outcome_warning VARCHAR")
            outcome_columns = {
                row[1]: row[2]
                for row in connection.execute("PRAGMA table_info('watch_outcomes')").fetchall()
            }
            if outcome_columns.get("close_price") != "VARCHAR":
                connection.execute("DROP INDEX IF EXISTS watch_outcomes_by_watch")
                connection.execute(
                    "ALTER TABLE watch_outcomes ALTER COLUMN close_price TYPE VARCHAR"
                )
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS watch_outcomes_by_watch
                       ON watch_outcomes (watch_id, retrieved_at)"""
                )

    def list(self) -> list[WatchRecord]:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """SELECT id, watch_key, ticker, root, side, expiration, strike,
                   terms_note, created_at, last_attempted_session, last_attempted_at,
                   last_error
                   FROM watches ORDER BY created_at DESC, id DESC""",
            )
        return [WatchRecord.model_validate(row) for row in found]

    def get_by_key(self, key: str) -> WatchRecord | None:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """SELECT id, watch_key, ticker, root, side, expiration, strike,
                   terms_note, created_at, last_attempted_session, last_attempted_at,
                   last_error
                   FROM watches WHERE watch_key = ?""",
                [key],
            )
        return WatchRecord.model_validate(found[0]) if found else None

    def add(
        self,
        key: str,
        ticker: str,
        root: str,
        side: str,
        expiry: date,
        strike: Decimal,
        created_at: datetime,
    ) -> tuple[WatchRecord, bool]:
        with connect(self.path) as connection:
            existing = rows(connection, "SELECT id FROM watches WHERE watch_key = ?", [key])
            if existing:
                created = False
            else:
                count = connection.execute("SELECT count(*) FROM watches").fetchone()[0]
                if count >= MAX_WATCHES:
                    raise HTTPException(status_code=429, detail="Watchlist limit reached")
                connection.execute(
                    """INSERT INTO watches (id, watch_key, ticker, root, side, expiration,
                       strike, terms_note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        uuid.uuid4().hex,
                        key,
                        ticker,
                        root,
                        side,
                        expiry,
                        strike,
                        TERMS_NOTE,
                        created_at,
                    ],
                )
                created = True
        item = self.get_by_key(key)
        assert item is not None
        return item, created

    def delete(self, watch_id: str) -> bool:
        with connect(self.path) as connection:
            found = rows(connection, "SELECT id FROM watches WHERE id = ?", [watch_id])
            if not found:
                return False
            connection.begin()
            try:
                connection.execute("DELETE FROM watch_outcomes WHERE watch_id = ?", [watch_id])
                connection.execute("DELETE FROM watches WHERE id = ?", [watch_id])
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def latest_outcomes(self) -> dict[str, OutcomeView]:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """SELECT o.watch_id, o.status, o.classification,
                   COALESCE(w.outcome_warning, o.reason) AS reason, o.source, o.session_date,
                   o.retrieved_at, o.close_price, o.terms_note, o.revised
                   FROM watch_outcomes o JOIN watches w ON w.id = o.watch_id
                   QUALIFY row_number() OVER (
                       PARTITION BY o.watch_id ORDER BY o.retrieved_at DESC, o.id DESC
                   ) = 1""",
            )
        latest: dict[str, OutcomeView] = {}
        for row in found:
            watch_id = str(row.pop("watch_id"))
            if watch_id not in latest:
                close = row.pop("close_price")
                latest[watch_id] = OutcomeView.model_validate(
                    {**row, "close_exact": str(close) if close is not None else None}
                )
        return latest

    def record_outcome(
        self, item: WatchRecord, result: OutcomeResult, attempted_session: date
    ) -> None:
        with connect(self.path) as connection:
            if not rows(connection, "SELECT id FROM watches WHERE id = ?", [item.id]):
                return
            previous = rows(
                connection,
                """SELECT status, close_price FROM watch_outcomes
                   WHERE watch_id = ? ORDER BY retrieved_at DESC, id DESC LIMIT 1""",
                [item.id],
            )
            connection.begin()
            try:
                # A failed later lookup does not erase a previously sourced result.
                keep_previous = (
                    (result.status == "pending" or result.preserve_prior)
                    and previous
                    and previous[0]["status"] == "provisional"
                )
                connection.execute(
                    "UPDATE watches SET outcome_warning = ? WHERE id = ?",
                    [result.reason if keep_previous else None, item.id],
                )
                if not keep_previous:
                    revised = bool(
                        previous
                        and previous[0]["status"] == "provisional"
                        and result.status == "provisional"
                        and previous[0]["close_price"] != str(result.close_price)
                    )
                    connection.execute(
                        """INSERT INTO watch_outcomes
                           (id, watch_id, status, classification, reason, source,
                            session_date, retrieved_at, close_price, terms_note, revised)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [
                            uuid.uuid4().hex,
                            item.id,
                            result.status,
                            result.classification,
                            result.reason,
                            result.source,
                            result.session_date,
                            result.retrieved_at,
                            str(result.close_price) if result.close_price is not None else None,
                            result.terms_note,
                            revised,
                        ],
                    )
                connection.execute(
                    """UPDATE watches SET last_attempted_session = ?,
                       last_attempted_at = ?, last_error = NULL
                       WHERE id = ?""",
                    [attempted_session, result.retrieved_at, item.id],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def mark_attempted(
        self,
        watch_id: str,
        session: date,
        error: str | None = None,
        *,
        attempted_at: datetime | None = None,
    ) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """UPDATE watches SET last_attempted_session = ?,
                   last_attempted_at = ?, last_error = ?
                   WHERE id = ?""",
                [None if error else session, attempted_at or datetime.now(UTC), error, watch_id],
            )


class WatchlistService:
    def __init__(self, data_dir: Path, provider: CloseProvider | None = None) -> None:
        self.store = WatchStore(data_dir)
        self.provider = provider or YahooCloseProvider()

    def item(
        self,
        record: WatchRecord,
        outcomes: dict[str, OutcomeView] | None = None,
        *,
        as_of: datetime | None = None,
    ) -> WatchItem:
        if outcomes is None:
            outcomes = self.store.latest_outcomes()
        outcome = outcomes.get(record.id)
        if outcome is None:
            completed = expiry_session_completed(
                record.expiration, as_of or datetime.now(UTC)
            )
            outcome = OutcomeView(
                status="pending",
                reason=(
                    "Awaiting first expiry close check"
                    if completed
                    else "Expiry trading session has not completed"
                ),
                session_date=session_on_or_before(record.expiration),
            )
        return WatchItem(
            id=record.id,
            ticker=record.ticker,
            root=record.root,
            side=record.side,
            expiration=record.expiration,
            strike_exact=strike_exact(record.strike),
            terms_note=record.terms_note,
            created_at=record.created_at,
            outcome=outcome,
        )

    def items(self, *, as_of: datetime | None = None) -> list[WatchItem]:
        outcomes = self.store.latest_outcomes()
        return [self.item(record, outcomes, as_of=as_of) for record in self.store.list()]

    def queue_refresh(
        self,
        jobs: JobManager,
        *,
        as_of: datetime,
        force: bool = False,
        retry_pending: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> Job | None:
        def due_items(session: date, now: datetime) -> list[WatchRecord]:
            outcomes = self.store.latest_outcomes()
            due: list[WatchRecord] = []
            for item in self.store.list():
                if not expiry_session_completed(item.expiration, now - OUTCOME_PUBLICATION_LAG):
                    continue
                if (
                    force
                    or item.last_attempted_session is None
                    or item.last_attempted_session < session
                ):
                    due.append(item)
                    continue
                outcome = outcomes.get(item.id)
                if retry_pending and (outcome is None or outcome.status == "pending"):
                    due.append(item)
            return due

        submitted_session = latest_completed_session(as_of - OUTCOME_PUBLICATION_LAG)
        submitted = due_items(submitted_session, as_of)
        if not submitted:
            return None

        ids = ",".join(sorted(item.id for item in submitted))
        batch_hash = hashlib.sha256(ids.encode()).hexdigest()[:16]
        coalesce_key = None if force else f"watch_refresh:{submitted_session}:{batch_hash}"

        def work(progress) -> None:
            # Resolve the close at execution; a queued job can cross a session boundary.
            execution_as_of = clock() if clock is not None else as_of
            if execution_as_of.tzinfo is None:
                execution_as_of = execution_as_of.replace(tzinfo=UTC)
            session = latest_completed_session(execution_as_of - OUTCOME_PUBLICATION_LAG)
            due = due_items(session, execution_as_of)
            for index, item in enumerate(due):
                progress(index / len(due), f"Checking {item.ticker} expiry {index + 1}/{len(due)}")
                item_session = session
                try:
                    item_as_of = clock() if clock is not None else execution_as_of
                    if item_as_of.tzinfo is None:
                        item_as_of = item_as_of.replace(tzinfo=UTC)
                    item_session = latest_completed_session(item_as_of - OUTCOME_PUBLICATION_LAG)
                    result = resolve_outcome(
                        ticker=item.ticker,
                        root=item.root,
                        side=item.side,
                        strike=item.strike,
                        expiration=item.expiration,
                        watched_at=item.created_at,
                        as_of=item_as_of,
                        provider=self.provider,
                        clock=clock,
                    )
                    self.store.record_outcome(item, result, item_session)
                except Exception:
                    LOG.exception("outcome refresh failed for %s", item.id)
                    self.store.mark_attempted(
                        item.id, item_session, "Outcome refresh failed",
                        attempted_at=clock() if clock is not None else item_as_of,
                    )
                progress((index + 1) / len(due), f"Checked {index + 1}/{len(due)} expiries")
            return None

        try:
            return jobs.submit("watch_refresh", work, coalesce_key=coalesce_key)
        except JobBusy as exc:
            if force:
                raise HTTPException(status_code=429, detail="Background job queue is busy") from exc
            return None


router = APIRouter()


@router.get("/api/jobs/{job_id}", response_model=Job)
def get_job(job_id: str, request: Request) -> Job:
    found = request.app.state.jobs.get(job_id)
    if found is None:
        raise HTTPException(status_code=404, detail="job not found")
    return found


def _now(request: Request) -> datetime:
    configured = request.app.state.clock()
    if configured.tzinfo is None:
        configured = configured.replace(tzinfo=UTC)
    return configured


def _queue(request: Request, *, force: bool = False, retry_pending: bool = False) -> Job | None:
    return request.app.state.watchlist.queue_refresh(
        request.app.state.jobs,
        as_of=_now(request),
        force=force,
        retry_pending=retry_pending,
        clock=request.app.state.clock,
    )


@router.get("/api/watchlist", response_model=WatchListResponse)
async def get_watchlist(request: Request) -> WatchListResponse:
    now = _now(request)
    items = request.app.state.watchlist.items(as_of=now)
    odds = request.app.state.market_odds
    active = [item for item in items if not expiry_session_completed(item.expiration, now)]
    odds.schedule(item.ticker for item in active)
    predictive = request.app.state.predictive_odds
    predictive.schedule(item.ticker for item in active)
    for item in items:
        if expiry_session_completed(item.expiration, now):
            item.market_odds = MarketOddsView(
                status="unavailable", reason="Expiry session completed; see outcome"
            )
            item.predictive_odds = PredictiveOddsView(
                status="unavailable", reason="expiry_completed"
            )
            item.hypothetical_risk = HypotheticalRiskView(
                reason="Expiry session completed"
            )
            continue
        result = quant_for_contract(
            odds,
            predictive,
            ticker=item.ticker,
            root=item.root,
            side=item.side,
            expiry=item.expiration,
            strike=Decimal(item.strike_exact),
            contract_since=first_session_after_completed(item.created_at),
            watched=True,
        )
        item.market_odds = result.market
        item.last_available_market_odds = result.last_available_market
        item.predictive_odds = result.predictive
        item.hypothetical_risk = result.risk
    return WatchListResponse(
        items=items,
        active_job=request.app.state.jobs.active("watch_refresh"),
    )


@router.post("/api/watchlist", response_model=WatchCreateResponse)
async def add_watch(request: Request, body: WatchCreate) -> WatchCreateResponse:
    parsed = parse_watch_key(body.watch_key)
    if parsed is None:
        raise HTTPException(status_code=400, detail="Invalid watch key")
    ticker, root, side, expiry_text, strike = parsed
    if normalize_ticker(ticker) != ticker:
        raise HTTPException(status_code=400, detail="Invalid watch key")
    universe = request.app.state.universe
    await universe.ensure()
    if not universe.available:
        raise HTTPException(status_code=503, detail="Ticker universe unavailable")
    if not universe.contains(ticker):
        raise HTTPException(status_code=404, detail="Unknown Nasdaq ticker")
    expiry = date.fromisoformat(expiry_text)
    if expiry_session_completed(expiry, _now(request)):
        raise HTTPException(status_code=409, detail="Expiry session has completed")
    try:
        chain = await request.app.state.service.get_current_chain(ticker)
    except NasdaqError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    matching: list[OptionQuote] = [
        row for row in chain.rows if row.expiration == expiry_text and row.strike == strike
    ]
    if len(matching) != 1:
        raise HTTPException(
            status_code=409, detail="Contract is absent or ambiguous in current chain"
        )
    row = matching[0]
    if row.identity_reason is not None or row.root != root or root != ticker:
        raise HTTPException(status_code=409, detail="Contract terms cannot be verified")
    liquidity = row.call_open_interest if side == "call" else row.put_open_interest
    if liquidity is None or liquidity < 5:
        raise HTTPException(status_code=409, detail="Contract is not eligible in current chain")
    if make_watch_key(ticker, root, side, expiry_text, row.strike) != body.watch_key:
        raise HTTPException(status_code=409, detail="Watch key differs from current chain")
    store: WatchStore = request.app.state.watchlist.store
    record, created = store.add(body.watch_key, ticker, root, side, expiry, strike, _now(request))
    request.app.state.market_odds.schedule([ticker])
    request.app.state.predictive_odds.schedule([ticker])
    job = _queue(request) if created else None
    item = request.app.state.watchlist.item(record, as_of=_now(request))
    result = quant_for_contract(
        request.app.state.market_odds,
        request.app.state.predictive_odds,
        ticker=ticker,
        root=root,
        side=side,
        expiry=expiry,
        strike=strike,
        contract_since=first_session_after_completed(record.created_at),
        watched=True,
    )
    item.market_odds = result.market
    item.last_available_market_odds = result.last_available_market
    item.predictive_odds = result.predictive
    item.hypothetical_risk = result.risk
    return WatchCreateResponse(
        item=item,
        created=created,
        job=job,
    )


@router.delete("/api/watchlist/{watch_id}", status_code=204)
async def delete_watch(request: Request, watch_id: str) -> None:
    if len(watch_id) != 32 or any(char not in "0123456789abcdef" for char in watch_id):
        raise HTTPException(status_code=404, detail="Watch not found")
    if not request.app.state.watchlist.store.delete(watch_id):
        raise HTTPException(status_code=404, detail="Watch not found")


@router.post("/api/watchlist/refresh", response_model=WatchRefreshResponse)
async def refresh_watchlist(request: Request) -> WatchRefreshResponse:
    return WatchRefreshResponse(job=_queue(request, force=True))
