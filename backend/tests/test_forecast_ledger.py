from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest
import duckdb
import stocksweeper.forecast.ledger as ledger_module

from options_api.contract_identity import make_watch_key
from options_api.outcomes import TERMS_NOTE, CloseHistory, classify, resolve_forecast_label
from stocksweeper.forecast.audit import (
    AuditCohort,
    AuditMember,
    freeze_audit_cohort,
    load_audit_snapshot,
    read_audit_cohort,
)
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.ledger import ForecastIssuance, ForecastLabel, ForecastLedger
from stocksweeper.forecast.labels import collect_matured_labels
from stocksweeper.forecast.physical_evaluation import crps
from stocksweeper.forecast.market import (
    CacheIntegrityError,
    ForecastPriceStore,
    price_hash,
)
from stocksweeper.storage.db import connect


T0 = datetime(2026, 9, 25, 20, tzinfo=UTC)
EXPIRY = date(2026, 10, 30)
KEY = make_watch_key("IREN", "IREN", "call", EXPIRY.isoformat(), Decimal("41"))


def issue(distribution_hash: str, **updates: object) -> ForecastIssuance:
    base = ForecastIssuance(
        contract_key=KEY,
        ticker="IREN",
        root="IREN",
        side="call",
        expiration=EXPIRY,
        expiry_session=EXPIRY,
        strike_exact="41.000",
        terms_note=TERMS_NOTE,
        contract_since=date(2026, 9, 25),
        input_session=date(2026, 9, 25),
        input_retrieved_at=T0,
        issued_at=T0 + timedelta(minutes=1),
        model_version="ewma-v1",
        method="lognormal_ewma",
        data_hash="a" * 64,
        distribution_hash=distribution_hash,
        price_basis="completed_close",
        spot_exact="44.125",
        status="available",
        itm_probability=0.6,
        otm_probability=0.4,
        atm_probability=0.0,
        unavailable_reason=None,
    )
    return replace(base, **updates)


def label(*, checked_at: datetime, status: str = "valid", close: str = "42") -> ForecastLabel:
    return ForecastLabel(
        contract_key=KEY,
        terms_note=TERMS_NOTE,
        expiry_session=EXPIRY,
        checked_at=checked_at,
        status=status,
        reason=None if status == "valid" else "close_sources_conflict",
        source="Nasdaq historical Close (Yahoo cross-check)" if status == "valid" else None,
        nasdaq_close_exact=close,
        yahoo_close_exact=close,
        selected_close_exact=close if status == "valid" else None,
        classification=classify("call", Decimal(close), Decimal("41"))
        if status == "valid"
        else None,
    )


def test_ledger_is_append_only_idempotent_and_latest_label_revision_controls_scoring(
    tmp_path,
) -> None:
    ledger = ForecastLedger(tmp_path)
    digest = ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    assert ledger.record(issue(digest))
    assert not ForecastLedger(tmp_path).record(issue(digest, issued_at=T0 + timedelta(hours=1)))
    assert ledger.record(
        issue(digest, model_version="student-t-v1", itm_probability=0.7, otm_probability=0.3)
    )
    assert ledger.record(
        issue(
            None,
            status="unavailable",
            model_version="garch-v1",
            itm_probability=None,
            otm_probability=None,
            atm_probability=None,
            unavailable_reason="nonconvergence",
            input_retrieved_at=None,
            data_hash=None,
            spot_exact=None,
        )
    )
    assert ledger.coverage()[-1]["attempts"] == 1
    assert ledger.record_label(label(checked_at=datetime(2026, 10, 31, 12, tzinfo=UTC)))
    assert len(ledger.scorable_rows()) == 2
    assert all(row["observed_itm"] for row in ledger.scorable_rows())
    assert "terminal_prices" not in ledger.scorable_rows()[0]
    assert ledger.crps_scores([(digest, "42")])[(digest, "42")] == pytest.approx(1.04)
    assert ledger.record_label(
        label(checked_at=datetime(2026, 11, 1, 12, tzinfo=UTC), status="excluded")
    )
    assert ledger.scorable_rows() == []
    assert ledger.record_label(label(checked_at=datetime(2026, 11, 2, 12, tzinfo=UTC), close="40"))
    rows = ledger.scorable_rows()
    assert len(rows) == 2
    assert all(row["observed_itm"] is False for row in rows)
    historical = list(
        ledger.iter_evaluation_rows(label_as_of=datetime(2026, 11, 1, 12, tzinfo=UTC))
    )
    assert {row["label_status"] for row in historical} == {"excluded"}
    with connect(tmp_path / "results.duckdb") as connection:
        assert connection.execute("SELECT count(*) FROM forecast_labels").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM forecast_issuances").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM watches").fetchone()[0] == 0


def test_thousands_of_contracts_share_one_bounded_distribution_read(tmp_path, monkeypatch) -> None:
    ledger = ForecastLedger(tmp_path)
    prices = tuple(float(index + 1) for index in range(4096))
    weights = (1 / 4096,) * 4096
    digest = ledger.record_distribution(prices, weights, T0)
    with connect(tmp_path / "results.duckdb") as connection:
        connection.execute(
            """INSERT INTO forecast_issuances (
               idempotency_key, contract_key, ticker, root, side, expiration,
               expiry_session, strike_exact, terms_note, input_session, issued_at,
               method, distribution_hash, status, provenance)
               SELECT 'issue-' || i,
                      'w1:IREN:IREN:call:2026-10-30:' ||
                        lpad(CAST(i + 1 AS VARCHAR), 3, '0') || '.000',
                      'IREN', 'IREN', 'call', ?, ?,
                      lpad(CAST(i + 1 AS VARCHAR), 3, '0') || '.000',
                      ?, ?, ?, 'lognormal_ewma', ?, 'available', 'as_issued'
               FROM range(2000) AS contracts(i)""",
            [EXPIRY, EXPIRY, TERMS_NOTE, T0.date(), T0, digest],
        )
        connection.execute(
            """INSERT INTO forecast_labels (
               idempotency_key, contract_key, terms_note, expiry_session,
               checked_at, status, selected_close_exact, classification)
               SELECT 'label-' || i,
                      'w1:IREN:IREN:call:2026-10-30:' ||
                        lpad(CAST(i + 1 AS VARCHAR), 3, '0') || '.000',
                      ?, ?, ?, 'valid', '42',
                      CASE WHEN i + 1 < 42 THEN 'itm'
                           WHEN i + 1 = 42 THEN 'atm' ELSE 'otm' END
               FROM range(2000) AS contracts(i)""",
            [TERMS_NOTE, EXPIRY, datetime(2026, 10, 31, tzinfo=UTC)],
        )
    attempts = ledger.evaluation_rows()
    assert len(attempts) == 2000
    assert all("terminal_prices" not in item and "weights" not in item for item in attempts)
    assert {item["label_status"] for item in attempts} == {"valid"}

    distribution_reads = []
    issuance_pages = []
    original_rows = ledger_module.rows

    def counted_rows(connection, sql, params=None):
        if "FROM forecast_distributions" in sql:
            distribution_reads.append(params)
        if "WITH issues AS MATERIALIZED" in sql:
            issuance_pages.append(params)
        return original_rows(connection, sql, params)

    monkeypatch.setattr(ledger_module, "rows", counted_rows)
    scored = list(ledger.iter_evaluation_rows(with_crps=True, page_size=128))
    expected = crps(prices, weights, 42)
    assert len(scored) == 2000
    assert all(item["crps"] == pytest.approx(expected) for item in scored)
    assert len(issuance_pages) >= 16
    assert distribution_reads == [[digest]]


def test_ledger_rejects_invalid_partition_and_unrecorded_distribution(tmp_path) -> None:
    ledger = ForecastLedger(tmp_path)
    with pytest.raises(ValueError, match="partition"):
        ledger.record(issue("b" * 64, itm_probability=0.8))
    with pytest.raises(ValueError, match="not recorded"):
        ledger.record(issue("b" * 64))
    with pytest.raises(ValueError, match="distribution"):
        ledger.record_distribution((44.0, 40.0), (0.5, 0.5), T0)
    with pytest.raises(ValueError, match="latency"):
        ledger.record(issue("b" * 64, prepare_ms=-0.1))
    with pytest.raises(ValueError, match="latency"):
        ledger.record(issue("b" * 64, lookup_ms=float("nan")))


def test_panel_coverage_counts_unrecorded_cohort_days_and_source_failures(
    tmp_path, monkeypatch
) -> None:
    members = tuple(
        AuditMember(f"A{index:02d}", "a" * 64, T0, tmp_path / "unused")
        for index in range(50)
    )
    cohort = AuditCohort(date(2026, 9, 25), T0, members)
    monkeypatch.setattr("stocksweeper.forecast.ledger.read_audit_cohort", lambda _path: cohort)
    digest = hashlib.sha256(
        "|".join(f"{member.ticker}:{member.data_hash}" for member in members).encode()
    ).hexdigest()
    bands = ("1", "2-5", "6-25")
    money = ("near ATM", "moderately ITM", "moderately OTM", "far ITM", "far OTM")
    failed_cells = [
        (
            date(2026, 9, 28), members[0].ticker, digest, T0, date(2026, 9, 25),
            datetime(2026, 9, 28, 15, tzinfo=UTC), band, moneyness, side,
            None, None, None, None, "missing", "chain_source_unavailable",
        )
        for band in bands
        for moneyness in money
        for side in ("call", "put")
    ]
    ledger = ForecastLedger(tmp_path)
    with connect(tmp_path / "results.duckdb") as connection:
        connection.executemany(
            "INSERT INTO forecast_panel_cells VALUES (" + ",".join("?" for _ in range(15)) + ")",
            failed_cells,
        )

    coverage = ledger.panel_coverage(as_of=datetime(2026, 9, 29, 22, tzinfo=UTC))
    assert coverage["cohort_status"] == "frozen"
    assert coverage["expected_sessions"] == 2
    for band in bands:
        entry = coverage["bands"][band]
        assert entry["expected_cells"] == 1000  # 2 sessions * 50 tickers * 10 cells.
        assert entry["recorded_cells"] == 10
        assert entry["status"] == {"missing": 10}
        assert entry["rejection_reasons"] == {"chain_source_unavailable": 10}
        assert entry["expected_cells"] - entry["recorded_cells"] == 990


def test_chain_batch_deduplicates_scenarios_and_is_atomic(tmp_path) -> None:
    class Distribution:
        prices = (40.0, 44.0)
        weights = (0.4, 0.6)

    ledger = ForecastLedger(tmp_path)
    pair = (issue(None), Distribution())
    second = (issue(None, model_version="student-t-v1"), Distribution())
    assert ledger.record_batch([pair, second]) == 2
    assert ledger.record_batch([pair, second]) == 0
    with connect(tmp_path / "results.duckdb") as connection:
        assert connection.execute("SELECT count(*) FROM forecast_distributions").fetchone()[0] == 1
    bad = (issue(None, model_version="bad", itm_probability=2.0), Distribution())
    with pytest.raises(ValueError, match="finite partition"):
        ledger.record_batch([(issue(None, model_version="would-rollback"), Distribution()), bad])
    assert len(ledger.evaluation_rows()) == 2


def test_intraday_window_keeps_first_available_across_restarts(tmp_path) -> None:
    class Distribution:
        prices = (40.0, 44.0)
        weights = (0.4, 0.6)

    initial = issue(None, snapshot_window="10:00", quote_time=T0, quote_source="Nasdaq underlying")
    newer_quote = replace(initial, quote_time=T0 + timedelta(seconds=30))
    ledger = ForecastLedger(tmp_path)
    assert ledger.record_batch([(initial, Distribution())]) == 1
    assert ForecastLedger(tmp_path).record_batch([(newer_quote, Distribution())]) == 0
    assert ledger.record_batch([(replace(initial, snapshot_window="13:00"), Distribution())]) == 1
    assert len(ledger.evaluation_rows()) == 2


def test_additive_evidence_schema_upgrade_preserves_old_label_rows(tmp_path) -> None:
    path = tmp_path / "results.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """CREATE TABLE forecast_labels (
               idempotency_key VARCHAR PRIMARY KEY, contract_key VARCHAR,
               expiry_session DATE, checked_at TIMESTAMPTZ, status VARCHAR,
               reason VARCHAR, source VARCHAR, nasdaq_close_exact VARCHAR,
               yahoo_close_exact VARCHAR, selected_close_exact VARCHAR,
               classification VARCHAR)"""
        )
        connection.execute(
            """INSERT INTO forecast_labels VALUES
               ('old', 'w1:IREN:IREN:call:2026-10-30:41.000', '2026-10-30',
                '2026-10-31 12:00:00+00', 'pending', 'source outage', NULL,
                NULL, NULL, NULL, NULL)"""
        )
    ledger = ForecastLedger(tmp_path)
    ledger.record_label(label(checked_at=datetime(2026, 11, 1, 12, tzinfo=UTC)))
    with connect(path) as connection:
        found = connection.execute("SELECT terms_note FROM forecast_labels ORDER BY checked_at")
        assert found.fetchall() == [(None,), (TERMS_NOTE,)]


def test_exact_nasdaq_label_crosschecks_yahoo_and_excludes_unsafe_sources() -> None:
    history = CloseHistory({EXPIRY: Decimal("41.0000001")}, frozenset(), frozenset(), True)
    kwargs = dict(
        ticker="IREN",
        root="IREN",
        side="call",
        strike=Decimal("41"),
        expiration=EXPIRY,
        contract_since=date(2026, 9, 25),
        as_of=datetime(2026, 10, 30, 21, tzinfo=UTC),
        nasdaq_close=Decimal("41.00"),
        yahoo_history=history,
    )
    equal = resolve_forecast_label(**kwargs)
    assert equal.status == "valid" and equal.classification == "atm"
    assert equal.selected_close == Decimal("41.00")
    conflict = resolve_forecast_label(**{**kwargs, "nasdaq_close": Decimal("41.01")})
    assert conflict.status == "excluded" and conflict.reason == "close_sources_conflict"
    split = resolve_forecast_label(
        **{**kwargs, "yahoo_history": replace(history, split_dates=frozenset({date(2026, 10, 1)}))}
    )
    assert split.reason == "split_affected"
    missing = resolve_forecast_label(**{**kwargs, "yahoo_history": None})
    assert missing.status == "pending" and missing.reason == "corporate_action_history_unavailable"
    put = resolve_forecast_label(
        **{
            **kwargs,
            "side": "put",
            "nasdaq_close": Decimal("40.99"),
            "yahoo_history": replace(history, closes={EXPIRY: Decimal("40.99")}),
        }
    )
    assert put.classification == "itm"


class Prices:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame

    def fetch(self, ticker: str, start: date | None, end: date) -> pl.DataFrame:
        return self.frame


def test_audit_cohort_is_fixed_verified_and_immutable(tmp_path) -> None:
    calendar = SessionCalendar()
    completed = date(2026, 9, 25)
    sessions = calendar.sessions(date(2026, 5, 1), completed)
    frame = pl.DataFrame(
        {
            "ts": sessions,
            "open": [100.0] * len(sessions),
            "high": [101.0] * len(sessions),
            "low": [99.0] * len(sessions),
            "close": [100.0] * len(sessions),
            "volume": [1000.0] * len(sessions),
            "dividends": [0.0] * len(sessions),
            "stock_splits": [0.0] * len(sessions),
        }
    )
    store = ForecastPriceStore(tmp_path, Prices(frame))
    for ticker in ("AAA", "BBB", "CCC"):
        store.update(ticker, completed)
    ledger = ForecastLedger(tmp_path)
    assert ledger.cache_retrieved_at("AAA", price_hash(frame), completed) is not None
    assert ledger.cache_retrieved_at("AAA", "f" * 64, completed) is None
    cohort = freeze_audit_cohort(
        tmp_path, ["CCC", "AAA", "BBB"], completed, size=2, calendar=calendar
    )
    assert len(cohort.members) == 2
    assert cohort.provenance == "immutable_current_vintage_replay"
    assert read_audit_cohort(tmp_path) == cohort
    assert load_audit_snapshot(tmp_path, cohort.members[0].ticker)[1].height == len(sessions)
    assert freeze_audit_cohort(tmp_path, ["AAA"], completed, size=2).members == cohort.members
    member = cohort.members[0]
    member.snapshot.chmod(0o600)
    member.snapshot.write_bytes(b"corrupted")
    with pytest.raises(CacheIntegrityError, match="invalid immutable audit cohort"):
        read_audit_cohort(tmp_path)


@pytest.mark.asyncio
async def test_matured_label_collection_records_outage_and_exact_revision(tmp_path) -> None:
    from options_api.models import HistoricalBar, HistoricalResponse

    expiry = date(2026, 7, 2)
    key = make_watch_key("IREN", "IREN", "call", expiry.isoformat(), Decimal("41"))
    issue_at = datetime(2026, 6, 30, 20, tzinfo=UTC)
    ledger = ForecastLedger(tmp_path)
    issued = replace(
        issue(None),
        contract_key=key,
        expiration=expiry,
        expiry_session=expiry,
        contract_since=date(2026, 6, 30),
        input_session=date(2026, 6, 30),
        input_retrieved_at=issue_at,
        issued_at=issue_at + timedelta(minutes=1),
    )

    class Distribution:
        prices = (40.0, 44.0)
        weights = (0.4, 0.6)

    assert ledger.record_batch([(issued, Distribution())]) == 1
    as_of = datetime(2026, 7, 3, 20, tzinfo=UTC)

    class Nasdaq:
        close = Decimal("41.00")
        outage = True

        async def get_history(self, ticker: str, from_date: str) -> HistoricalResponse:
            if self.outage:
                raise RuntimeError("source outage")
            return HistoricalResponse(
                ticker=ticker,
                fetched_at=as_of,
                from_cache=False,
                bars=[HistoricalBar(date=expiry, close=self.close)],
            )

    class Yahoo:
        def fetch(self, ticker: str, start: date, end: date) -> CloseHistory:
            return CloseHistory({expiry: Decimal("41.0000001")}, frozenset(), frozenset(), True)

    nasdaq = Nasdaq()
    assert await collect_matured_labels(
        ledger, nasdaq, yahoo=Yahoo(), as_of=as_of, clock=lambda: as_of
    ) == {"nasdaq_source_unavailable": 1}
    assert ledger.scorable_rows() == []
    nasdaq.outage = False
    later = as_of + timedelta(hours=2)
    assert await collect_matured_labels(
        ledger, nasdaq, yahoo=Yahoo(), as_of=later, clock=lambda: later
    ) == {"valid": 1}
    rows = ledger.scorable_rows()
    assert len(rows) == 1 and rows[0]["classification"] == "atm"
    assert rows[0]["selected_close_exact"] == "41.00"


@pytest.mark.asyncio
async def test_matured_labels_fetch_each_ticker_once_for_many_contracts(tmp_path) -> None:
    from options_api.models import HistoricalBar, HistoricalResponse

    ledger = ForecastLedger(tmp_path)
    expiries = (date(2026, 7, 2), date(2026, 7, 10))
    issued_at = datetime(2026, 6, 30, 20, tzinfo=UTC)

    class Distribution:
        prices = (40.0, 44.0)
        weights = (0.4, 0.6)

    batch = []
    for index in range(50):
        expiry = expiries[index // 25]
        strike = Decimal(20 + index)
        batch.append(
            (
                replace(
                    issue(None),
                    contract_key=make_watch_key("IREN", "IREN", "call", expiry.isoformat(), strike),
                    expiration=expiry,
                    expiry_session=expiry,
                    strike_exact=str(strike),
                    contract_since=date(2026, 6, 30),
                    input_session=date(2026, 6, 30),
                    input_retrieved_at=issued_at,
                    issued_at=issued_at + timedelta(minutes=1),
                ),
                Distribution(),
            )
        )
    assert ledger.record_batch(batch) == 50
    as_of = datetime(2026, 7, 11, 20, tzinfo=UTC)

    class Nasdaq:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def get_history(self, ticker: str, from_date: str) -> HistoricalResponse:
            self.calls.append((ticker, from_date))
            return HistoricalResponse(
                ticker=ticker,
                fetched_at=as_of,
                from_cache=False,
                bars=[HistoricalBar(date=expiry, close=Decimal("41.00")) for expiry in expiries],
            )

    class Yahoo:
        def __init__(self) -> None:
            self.calls: list[tuple[str, date, date]] = []

        def fetch(self, ticker: str, start: date, end: date) -> CloseHistory:
            self.calls.append((ticker, start, end))
            return CloseHistory(
                {expiry: Decimal("41.0000001") for expiry in expiries},
                frozenset(),
                frozenset(),
                True,
            )

    nasdaq, yahoo = Nasdaq(), Yahoo()
    assert await collect_matured_labels(
        ledger, nasdaq, yahoo=yahoo, as_of=as_of, limit=50, clock=lambda: as_of
    ) == {"valid": 50}
    assert nasdaq.calls == [("IREN", "2026-07-02")]
    assert yahoo.calls == [("IREN", date(2026, 6, 30), date(2026, 7, 12))]
    assert len(ledger.scorable_rows()) == 50


@pytest.mark.asyncio
async def test_conflicting_duplicate_nasdaq_bars_exclude_exact_label(tmp_path) -> None:
    from options_api.models import HistoricalBar, HistoricalResponse

    expiry = date(2026, 7, 2)
    issued_at = datetime(2026, 6, 30, 20, tzinfo=UTC)
    ledger = ForecastLedger(tmp_path)
    contract = replace(
        issue(None),
        contract_key=make_watch_key("IREN", "IREN", "call", expiry.isoformat(), Decimal(41)),
        expiration=expiry,
        expiry_session=expiry,
        contract_since=date(2026, 6, 30),
        input_session=date(2026, 6, 30),
        input_retrieved_at=issued_at,
        issued_at=issued_at + timedelta(minutes=1),
    )

    class Distribution:
        prices = (40.0, 44.0)
        weights = (0.4, 0.6)

    ledger.record_batch([(contract, Distribution())])
    as_of = datetime(2026, 7, 3, 20, tzinfo=UTC)

    class Nasdaq:
        async def get_history(self, _ticker, _from_date):
            return HistoricalResponse(
                ticker="IREN",
                fetched_at=as_of,
                from_cache=False,
                bars=[
                    HistoricalBar(date=expiry, close=Decimal("41.00")),
                    HistoricalBar(date=expiry, close=Decimal("41.01")),
                ],
            )

    class Yahoo:
        def fetch(self, _ticker, _start, _end):
            return CloseHistory({expiry: Decimal("41.00")}, frozenset(), frozenset(), True)

    assert await collect_matured_labels(
        ledger, Nasdaq(), yahoo=Yahoo(), as_of=as_of, clock=lambda: as_of
    ) == {"nasdaq_close_ambiguous": 1}
    assert ledger.scorable_rows() == []
    assert ledger.evaluation_rows()[0]["label_status"] == "excluded"
