"""Offline provenance checks: frozen prices, missed windows and SEC knowledge time."""

from __future__ import annotations

import json
import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import polars as pl
import pytest

from options_api.contract_identity import make_watch_key
from stocksweeper.forecast.audit import freeze_audit_cohort
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.capture_windows import capture_window_counts, reconcile_capture_windows
from stocksweeper.forecast.market import CacheIntegrityError, ForecastPriceStore, price_hash
from stocksweeper.forecast.provenance import (
    SourceRightsUnverified,
    freeze_price_vintage,
    freeze_training_cohort,
    load_training_cohort,
    read_price_vintage,
)
from stocksweeper.forecast.sec_events import (
    capture_recent_sec_schedules,
    effective_forward_schedules,
    known_forward_schedules,
    parse_actual_results,
    parse_forward_schedule,
    record_actual,
    record_schedule,
    verified_past_earnings_events,
)
from stocksweeper.storage.db import connect, rows

SESSION = date(2026, 9, 25)


def _prices(last: date = SESSION, count: int = 501) -> pl.DataFrame:
    days = SessionCalendar().sessions(date(2023, 1, 1), last)[-count:]
    close = [100 + i / 100 for i in range(count)]
    return pl.DataFrame(
        {
            "ts": days,
            "open": close,
            "high": [x + 1 for x in close],
            "low": [x - 1 for x in close],
            "close": close,
            "volume": [1000.0] * count,
            "dividends": [0.0] * count,
            "stock_splits": [0.0] * count,
        }
    )


class Provider:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame

    def fetch(self, ticker, start, end):
        frame = self.frame.filter(pl.col("ts") <= end)
        return frame if start is None else frame.filter(pl.col("ts") >= start)


def test_local_vintages_preserve_revisions_and_reject_tampering(tmp_path):
    provider = Provider(_prices())
    store = ForecastPriceStore(tmp_path, provider)
    store.update("AAPL", SESSION)
    first, first_hash = freeze_price_vintage(tmp_path, "AAPL", SESSION)
    assert first.height == 501
    provider.frame = provider.frame.with_columns(
        pl.when(pl.col("ts") == SESSION).then(106.0).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.col("ts") == SESSION).then(107.0).otherwise(pl.col("high")).alias("high"),
    )
    store.update("AAPL", SESSION, full_refresh=True)
    _, second_hash = freeze_price_vintage(tmp_path, "AAPL", SESSION)
    assert first_hash != second_hash
    assert read_price_vintage(tmp_path, "AAPL", SESSION, first_hash)[0].equals(first)
    path = (
        tmp_path / "forecast" / "vintages" / "AAPL" / SESSION.isoformat() / f"{first_hash}.parquet"
    )
    path.chmod(0o600)
    path.write_bytes(b"changed")
    with pytest.raises(CacheIntegrityError):
        read_price_vintage(tmp_path, "AAPL", SESSION, first_hash)


def test_vintage_freeze_rejects_cache_manifest_race(tmp_path, monkeypatch):
    ForecastPriceStore(tmp_path, Provider(_prices())).update("AAPL", SESSION)
    original_read = ForecastPriceStore.read

    def raced_read(store, ticker):
        frame = original_read(store, ticker)
        manifest = store.path(ticker).with_suffix(".json")
        metadata = json.loads(manifest.read_text())
        metadata["hash"] = "0" * 64
        manifest.write_text(json.dumps(metadata))
        return frame

    monkeypatch.setattr(ForecastPriceStore, "read", raced_read)
    with pytest.raises(CacheIntegrityError, match="changed while freezing"):
        freeze_price_vintage(tmp_path, "AAPL", SESSION)


def test_frozen_vintage_requires_its_manifest_hash(tmp_path):
    ForecastPriceStore(tmp_path, Provider(_prices())).update("AAPL", SESSION)
    _, digest = freeze_price_vintage(tmp_path, "AAPL", SESSION)
    manifest = tmp_path / "forecast" / "vintages" / "AAPL" / SESSION.isoformat() / f"{digest}.json"
    metadata = json.loads(manifest.read_text())
    metadata.pop("manifest_hash")
    metadata["source_retrieved_at"] = datetime(2026, 9, 27, tzinfo=UTC).isoformat()
    manifest.chmod(0o600)
    manifest.write_text(json.dumps(metadata))
    with pytest.raises(CacheIntegrityError, match="invalid immutable price vintage"):
        read_price_vintage(tmp_path, "AAPL", SESSION, digest)


def _signed(metadata: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_rights_approved_synthetic_cohort_loads_for_trainer_without_mocking(tmp_path):
    from stocksweeper.forecast.pooled_ngboost import (
        _training_rows, load_pooled_model, train_pooled_model,
    )

    ForecastPriceStore(tmp_path, Provider(_prices())).update("AAPL", SESSION)
    audit = freeze_audit_cohort(tmp_path, ["AAPL"], SESSION, size=1)
    source = "Project synthetic fixture"
    license_reference = "project-owned-synthetic-test-data-v1"
    frame = _prices()
    data_hash = price_hash(frame)
    observed = datetime(2026, 9, 26, 12, tzinfo=UTC).isoformat()
    members = []
    for index in range(20):
        ticker = f"T{chr(65 + index)}A"
        root = tmp_path / "forecast" / "vintages" / ticker / SESSION.isoformat()
        root.mkdir(parents=True)
        frame.write_parquet(root / f"{data_hash}.parquet")
        vintage: dict[str, object] = {
            "ticker": ticker,
            "through_session": SESSION.isoformat(),
            "data_hash": data_hash,
            "source_hash": data_hash,
            "source": source,
            "source_name": source,
            "license_reference": license_reference,
            "source_retrieved_at": observed,
            "frozen_at": observed,
            "price_basis": "split-normalized, dividend-unadjusted",
            "provenance": "immutable_current_vintage_training",
            "rights_status": "approved_for_training",
        }
        vintage["manifest_hash"] = _signed(vintage)
        (root / f"{data_hash}.json").write_text(json.dumps(vintage))
        members.append({"ticker": ticker, "data_hash": data_hash})
    cohort: dict[str, object] = {
        "provenance": "immutable_current_vintage_training",
        "rights_status": "approved_for_training",
        "source": source,
        "source_name": source,
        "license_reference": license_reference,
        "audit_cohort_hash": _signed(
            {"members": sorted((member.ticker, member.data_hash) for member in audit.members)}
        ),
        "completed_session": SESSION.isoformat(),
        "size": len(members),
        "members": members,
        "frozen_at": observed,
    }
    cohort["manifest_hash"] = _signed(cohort)
    manifest = tmp_path / "forecast" / "training" / "cohort.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(cohort))
    loaded = load_training_cohort(tmp_path)
    assert len(loaded) == 20
    inputs, targets, through, _ = _training_rows(loaded)
    assert len(inputs) >= 5_000 and len(targets) == len(inputs) and through == SESSION
    artifact = train_pooled_model(tmp_path)
    assert artifact.is_file()
    assert load_pooled_model(tmp_path)["cohort_manifest_hash"] == cohort["manifest_hash"]
    cohort["license_reference"] = "another-source-right"
    cohort["manifest_hash"] = _signed(
        {key: value for key, value in cohort.items() if key != "manifest_hash"}
    )
    manifest.write_text(json.dumps(cohort))
    with pytest.raises(CacheIntegrityError, match="invalid immutable training cohort"):
        load_training_cohort(tmp_path)
    with pytest.raises(CacheIntegrityError, match="invalid immutable training cohort"):
        load_pooled_model(tmp_path)


def test_training_cohort_is_disjoint_and_rights_unqualified(tmp_path):
    for ticker in ("AAPL", "MSFT", "NVDA"):
        ForecastPriceStore(tmp_path, Provider(_prices())).update(ticker, SESSION)
    audit = freeze_audit_cohort(tmp_path, ["AAPL", "MSFT", "NVDA"], SESSION, size=1)
    members = freeze_training_cohort(tmp_path, ["AAPL", "MSFT", "NVDA"], SESSION, size=2)
    assert {ticker for ticker, _, _ in members}.isdisjoint({item.ticker for item in audit.members})
    assert len(load_training_cohort(tmp_path, require_training_rights=False)) == 2
    with pytest.raises(SourceRightsUnverified, match="training_rights_unverified"):
        load_training_cohort(tmp_path)
    with pytest.raises(ValueError, match="another size or session"):
        freeze_training_cohort(tmp_path, ["AAPL", "MSFT", "NVDA"], SESSION, size=1)
    audit_manifest = tmp_path / "forecast" / "audit" / "cohort.json"
    audit_missing = audit_manifest.with_suffix(".missing")
    audit_manifest.rename(audit_missing)
    try:
        with pytest.raises(CacheIntegrityError, match="invalid immutable training cohort"):
            load_training_cohort(tmp_path, require_training_rights=False)
    finally:
        audit_missing.rename(audit_manifest)
    manifest = tmp_path / "forecast" / "training" / "cohort.json"
    contents = json.loads(manifest.read_text())
    assert contents["rights_status"] == "training_rights_unverified"
    contents["size"] = 1
    manifest.chmod(0o600)
    manifest.write_text(json.dumps(contents))
    with pytest.raises(CacheIntegrityError):
        load_training_cohort(tmp_path, require_training_rights=False)


def test_missing_bar_and_recent_split_reject_frozen_training_history(tmp_path):
    frame = _prices()
    missing = frame.filter(pl.col("ts") != frame["ts"][200])
    ForecastPriceStore(tmp_path / "gap", Provider(missing)).update("AAPL", SESSION)
    with pytest.raises(ValueError, match="contiguous split-safe"):
        freeze_price_vintage(tmp_path / "gap", "AAPL", SESSION)
    split = frame.with_columns(
        pl.when(pl.col("ts") == frame["ts"][300])
        .then(2.0)
        .otherwise(pl.col("stock_splits"))
        .alias("stock_splits")
    )
    ForecastPriceStore(tmp_path / "split", Provider(split)).update("AAPL", SESSION)
    with pytest.raises(ValueError, match="contiguous split-safe"):
        freeze_price_vintage(tmp_path / "split", "AAPL", SESSION)


@dataclass(frozen=True)
class Watch:
    watch_key: str
    created_at: datetime
    expiration: date


def test_capture_denominator_recovers_missed_window_then_captured_revision(tmp_path):
    key = make_watch_key("AAPL", "AAPL", "call", "2026-10-30", Decimal("100"))
    watch = Watch(key, datetime(2026, 9, 24, 20, tzinfo=UTC), date(2026, 10, 30))
    now = datetime(2026, 9, 25, 15, 6, tzinfo=UTC)
    first = reconcile_capture_windows(tmp_path, [watch], now)
    assert first == {"expected": 1, "captured": 0, "missed": 1, "pending": 0}
    assert reconcile_capture_windows(tmp_path, [watch], now) == first
    with connect(tmp_path / "results.duckdb") as connection:
        connection.execute(
            """INSERT INTO forecast_issuances
               (idempotency_key, contract_key, ticker, root, side, expiration,
                expiry_session, strike_exact, terms_note, issued_at, status,
                provenance, snapshot_window)
               VALUES (?, ?, 'AAPL', 'AAPL', 'call', '2026-10-30', '2026-10-30',
                       '100.000', 'standard', ?, 'unavailable', 'as_issued', '10:00')""",
            ["a" * 64, key, datetime(2026, 9, 25, 14, 1, tzinfo=UTC)],
        )
    second = reconcile_capture_windows(tmp_path, [watch], now)
    assert second["captured"] == 1 and second["missed"] == 0
    assert capture_window_counts(
        tmp_path, datetime(2026, 9, 25, 14, tzinfo=UTC), now
    ) == {"expected": 1, "captured": 1, "missed": 0, "pending": 0}
    with connect(tmp_path / "results.duckdb") as connection:
        events = rows(connection, "SELECT event FROM forecast_capture_window_events")
    assert sorted(row["event"] for row in events) == ["captured", "expected", "missed"]


def test_removed_watch_still_resolves_persisted_expected_window(tmp_path):
    key = make_watch_key("AAPL", "AAPL", "call", "2026-10-30", Decimal("100"))
    watch = Watch(key, datetime(2026, 9, 24, 20, tzinfo=UTC), date(2026, 10, 30))
    pending = reconcile_capture_windows(
        tmp_path, [watch], datetime(2026, 9, 25, 14, 2, tzinfo=UTC)
    )
    assert pending == {"expected": 1, "captured": 0, "missed": 0, "pending": 1}
    assert capture_window_counts(
        tmp_path,
        datetime(2026, 9, 25, 14, tzinfo=UTC),
        datetime(2026, 9, 25, 14, 2, tzinfo=UTC),
    ) == pending
    resolved = reconcile_capture_windows(
        tmp_path, [], datetime(2026, 9, 25, 14, 6, tzinfo=UTC)
    )
    assert resolved == {"expected": 1, "captured": 0, "missed": 1, "pending": 0}
    with connect(tmp_path / "results.duckdb") as connection:
        events = rows(connection, "SELECT event FROM forecast_capture_window_events")
    assert sorted(row["event"] for row in events) == ["expected", "missed"]
    assert capture_window_counts(
        tmp_path,
        datetime(2026, 9, 25, 14, tzinfo=UTC),
        datetime(2026, 9, 25, 14, 6, tzinfo=UTC),
    ) == resolved


def test_capture_counts_are_read_only_and_reject_unbounded_interval(tmp_path):
    start = datetime(2026, 9, 25, 14, tzinfo=UTC)
    assert capture_window_counts(tmp_path, start, start) == {
        "expected": 0, "captured": 0, "missed": 0, "pending": 0,
    }
    assert not (tmp_path / "results.duckdb").exists()
    with connect(tmp_path / "results.duckdb"):
        # The live app can hold a write-capable connection during evidence reads.
        assert capture_window_counts(tmp_path, start, start)["expected"] == 0
    with pytest.raises(ValueError, match="at most 35 days"):
        capture_window_counts(tmp_path, start - timedelta(days=36), start)


def test_early_close_does_not_expect_closed_window(tmp_path):
    watch = Watch(
        make_watch_key("AAPL", "AAPL", "put", "2026-12-04", Decimal("100")),
        datetime(2026, 11, 26, 20, tzinfo=UTC),
        date(2026, 12, 4),
    )
    # 2026-11-27 closes at 13:00 ET: 13:00 and 15:30 are not capture windows.
    report = reconcile_capture_windows(tmp_path, [watch], datetime(2026, 11, 27, 21, tzinfo=UTC))
    assert report["expected"] == 1 and report["missed"] == 1


def test_capture_accounting_indexes_issuances_by_window(tmp_path, monkeypatch):
    from stocksweeper.forecast import capture_windows

    watches = [
        Watch(
            make_watch_key("AAPL", "AAPL", "call", "2026-10-30", Decimal(index + 1)),
            datetime(2026, 9, 24, 20, tzinfo=UTC),
            date(2026, 10, 30),
        )
        for index in range(128)
    ]
    reads = [0]

    class Tracked(dict):
        def __getitem__(self, field):
            reads[0] += 1
            return super().__getitem__(field)

    issued = [
        Tracked(
            contract_key=watch.watch_key,
            snapshot_window="10:00",
            issued_at=datetime(2026, 9, 25, 14, 1, tzinfo=UTC),
        )
        for watch in watches
    ]
    monkeypatch.setattr(
        capture_windows,
        "rows",
        lambda _connection, sql, _params: [] if "forecast_capture_window_events" in sql else issued,
    )
    counts = reconcile_capture_windows(tmp_path, watches, datetime(2026, 9, 25, 15, 6, tzinfo=UTC))
    assert counts["captured"] == 128
    assert reads[0] <= 128 * 4  # One pass through issuances, independent of watch count.


def _announcement(day: str) -> bytes:
    return (
        f"<html><p>Acme will report third quarter 2026 financial results on {day} "
        "at 4:30 PM Eastern Time.</p></html>"
    ).encode()


def _results(day: str) -> bytes:
    return (
        f"<html><p>On {day}, Acme announced its third quarter 2026 financial results.</p></html>"
    ).encode()


def test_sec_sept_abbreviation_is_accepted_for_schedule_and_actual():
    accepted = datetime(2026, 9, 1, 16, tzinfo=UTC)
    schedule = parse_forward_schedule(
        "ACME", 12345, "0000012345-26-000001", accepted,
        accepted + timedelta(minutes=5), "ex99-1.htm", _announcement("Sept. 30, 2026"),
    )
    assert schedule is not None and schedule.event_date == date(2026, 9, 30)
    actual_accepted = datetime(2026, 10, 1, 16, tzinfo=UTC)
    actual = parse_actual_results(
        "ACME", 12345, "0000012345-26-000002", actual_accepted,
        actual_accepted + timedelta(minutes=5), "ex99-1.htm", _results("Sept. 30, 2026"),
    )
    assert actual is not None and actual.event_date == date(2026, 9, 30)


def test_sec_dotted_meridiem_preserves_announced_time():
    accepted = datetime(2026, 9, 28, 16, tzinfo=UTC)
    for meridiem, hour in (("a.m.", 9), ("p.m.", 21)):
        schedule = parse_forward_schedule(
            "ACME", 12345, "0000012345-26-000001", accepted,
            accepted + timedelta(minutes=5), "ex99-1.htm",
            _announcement("November 5, 2026").replace(b"PM", meridiem.encode()),
        )
        assert schedule is not None
        assert schedule.event_at == datetime(2026, 11, 5, hour, 30, tzinfo=UTC)


def test_sec_dotted_meridiem_does_not_take_next_sentence_time():
    accepted = datetime(2026, 9, 28, 16, tzinfo=UTC)
    for separator in (b" ", b""):
        schedule = parse_forward_schedule(
            "ACME", 12345, "0000012345-26-000001", accepted,
            accepted + timedelta(minutes=5), "ex99-1.htm",
            b"<p>Acme will report earnings on November 5, 2026 at 4 p.m."
            + separator + b"Conference call at 8:00 AM ET.</p>",
        )
        assert schedule is not None and schedule.event_at is None


def test_sec_schedule_revisions_obey_retrieval_knowledge_time(tmp_path):
    accepted = datetime(2026, 9, 28, 16, tzinfo=UTC)
    first = parse_forward_schedule(
        "ACME",
        12345,
        "0000012345-26-000001",
        accepted,
        accepted + timedelta(minutes=5),
        "ex99-1.htm",
        _announcement("November 5, 2026"),
    )
    assert first is not None and first.series_key == "2026Q3"
    assert first.event_at == datetime(2026, 11, 5, 21, 30, tzinfo=UTC)
    assert record_schedule(tmp_path, first)
    assert not record_schedule(tmp_path, first)
    second = parse_forward_schedule(
        "ACME",
        12345,
        "0000012345-26-000002",
        accepted + timedelta(days=1),
        accepted + timedelta(days=1, minutes=5),
        "ex99-1.htm",
        _announcement("November 6, 2026"),
    )
    assert second is not None and record_schedule(tmp_path, second)
    before = known_forward_schedules(
        tmp_path, "ACME", accepted + timedelta(minutes=4), date(2026, 11, 30)
    )
    middle = known_forward_schedules(
        tmp_path, "ACME", accepted + timedelta(hours=1), date(2026, 11, 30)
    )
    after = known_forward_schedules(
        tmp_path, "ACME", accepted + timedelta(days=2), date(2026, 11, 30)
    )
    assert before == ()
    assert [row["event_date"] for row in middle] == [date(2026, 11, 5)]
    assert [row["event_date"] for row in after] == [date(2026, 11, 5), date(2026, 11, 6)]
    assert (
        parse_forward_schedule(
            "ACME",
            12345,
            "0000012345-26-000003",
            accepted,
            accepted + timedelta(minutes=5),
            "ex99-1.htm",
            b"<p>Acme reported earnings today.</p>",
        )
        is None
    )


def test_realized_results_require_explicit_release_and_prior_schedule(tmp_path):
    accepted = datetime(2026, 9, 28, 16, tzinfo=UTC)
    schedule = parse_forward_schedule(
        "ACME",
        12345,
        "0000012345-26-000001",
        accepted,
        accepted + timedelta(minutes=5),
        "ex99-1.htm",
        _announcement("November 5, 2026"),
    )
    assert schedule is not None and record_schedule(tmp_path, schedule)
    actual_accepted = datetime(2026, 11, 6, 12, tzinfo=UTC)
    actual = parse_actual_results(
        "ACME",
        12345,
        "0000012345-26-000002",
        actual_accepted,
        actual_accepted + timedelta(minutes=5),
        "ex99-1.htm",
        _results("November 5, 2026"),
    )
    assert actual is not None and record_actual(tmp_path, actual)
    assert not record_actual(tmp_path, actual)
    before = verified_past_earnings_events(tmp_path, "ACME", actual_accepted + timedelta(minutes=4))
    after = verified_past_earnings_events(tmp_path, "ACME", actual_accepted + timedelta(hours=1))
    assert before == ()
    assert len(after) == 1
    assert after[0]["event_date"] == date(2026, 11, 5)
    assert after[0]["schedule_accession"] == schedule.accession
    assert (
        parse_actual_results(
            "ACME",
            12345,
            "0000012345-26-000003",
            actual_accepted,
            actual_accepted + timedelta(minutes=5),
            "ex99-1.htm",
            b"<p>Acme reported results.</p>",
        )
        is None
    )


def test_same_ticker_different_cik_cannot_validate_actual_release(tmp_path):
    accepted = datetime(2026, 9, 28, 16, tzinfo=UTC)
    schedule = parse_forward_schedule(
        "ACME", 12345, "0000012345-26-000001", accepted,
        accepted + timedelta(minutes=5), "ex99-1.htm", _announcement("November 5, 2026"),
    )
    assert schedule is not None and record_schedule(tmp_path, schedule)
    actual_accepted = datetime(2026, 11, 6, 12, tzinfo=UTC)
    actual = parse_actual_results(
        "ACME", 54321, "0000054321-26-000001", actual_accepted,
        actual_accepted + timedelta(minutes=5), "ex99-1.htm", _results("November 5, 2026"),
    )
    assert actual is not None and record_actual(tmp_path, actual)
    assert (
        verified_past_earnings_events(tmp_path, "ACME", actual_accepted + timedelta(hours=1))
        == ()
    )


def test_later_schedule_revision_does_not_validate_old_event_date(tmp_path):
    accepted = datetime(2026, 9, 28, 16, tzinfo=UTC)
    for index, day in enumerate(("November 5, 2026", "November 6, 2026"), start=1):
        timestamp = accepted + timedelta(days=index - 1)
        schedule = parse_forward_schedule(
            "ACME",
            12345,
            f"0000012345-26-{index:06d}",
            timestamp,
            timestamp + timedelta(minutes=5),
            "ex99-1.htm",
            _announcement(day),
        )
        assert schedule is not None and record_schedule(tmp_path, schedule)
    current = effective_forward_schedules(
        tmp_path, "ACME", accepted + timedelta(days=2), date(2026, 11, 30)
    )
    assert [row["event_date"] for row in current] == [date(2026, 11, 6)]
    actual_time = datetime(2026, 11, 6, 12, tzinfo=UTC)
    old_date_actual = parse_actual_results(
        "ACME",
        12345,
        "0000012345-26-000003",
        actual_time,
        actual_time + timedelta(minutes=5),
        "ex99-1.htm",
        _results("November 5, 2026"),
    )
    assert old_date_actual is not None and record_actual(tmp_path, old_date_actual)
    assert verified_past_earnings_events(tmp_path, "ACME", actual_time + timedelta(hours=1)) == ()


def test_expired_revision_does_not_leave_an_older_future_schedule_active(tmp_path):
    accepted = datetime(2026, 10, 1, 16, tzinfo=UTC)
    for index, day in enumerate(("November 10, 2026", "November 3, 2026"), start=1):
        observed = accepted + timedelta(days=index - 1)
        schedule = parse_forward_schedule(
            "ACME",
            12345,
            f"0000012345-26-{index:06d}",
            observed,
            observed + timedelta(minutes=5),
            "ex99-1.htm",
            _announcement(day),
        )
        assert schedule is not None and record_schedule(tmp_path, schedule)
    as_of = datetime(2026, 11, 5, 16, tzinfo=UTC)
    assert [row["event_date"] for row in known_forward_schedules(
        tmp_path, "ACME", as_of, date(2026, 11, 30)
    )] == [date(2026, 11, 10)]
    assert effective_forward_schedules(tmp_path, "ACME", as_of, date(2026, 11, 30)) == ()


def test_schedule_does_not_take_an_unrelated_filing_time_as_event_time():
    accepted = datetime(2026, 9, 28, 16, tzinfo=UTC)
    content = (
        b"<p>Filed at 9:00 AM ET. Acme will report third quarter 2026 "
        b"financial results on November 5, 2026.</p>"
    )
    schedule = parse_forward_schedule(
        "ACME", 12345, "0000012345-26-000001", accepted,
        accepted + timedelta(minutes=5), "ex99-1.htm", content,
    )
    assert schedule is not None and schedule.event_at is None


def test_sec_fetch_is_bounded_and_checks_cik_and_filing_path(tmp_path):
    accepted = "2026-09-28T12:00:00"
    accession = "0000012345-26-000001"
    submissions = {
        "cik": 12345,
        "tickers": ["ACME"],
        "filings": {
            "recent": {
                "form": ["8-K"],
                "accessionNumber": [accession],
                "primaryDocument": ["announcement.htm"],
                "acceptanceDateTime": [accepted],
            }
        },
    }
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if request.url.host == "data.sec.gov":
            return httpx.Response(200, json=submissions)
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, json={"directory": {"item": []}})
        return httpx.Response(200, content=_announcement("November 5, 2026"))

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert (
        capture_recent_sec_schedules(
            tmp_path,
            "ACME",
            12345,
            "HyperOptions test@example.com",
            now=datetime(2026, 9, 28, 17, tzinfo=UTC),
            client=client,
        )
        == 1
    )
    assert urls == [
        "https://data.sec.gov/submissions/CIK0000012345.json",
        "https://www.sec.gov/Archives/edgar/data/12345/000001234526000001/announcement.htm",
        "https://www.sec.gov/Archives/edgar/data/12345/000001234526000001/index.json",
    ]
    assert (
        capture_recent_sec_schedules(
            tmp_path,
            "ACME",
            12345,
            "HyperOptions test@example.com",
            now=datetime(2026, 9, 28, 17, tzinfo=UTC),
            client=client,
        )
        == 0
    )
    submissions["cik"] = 54321
    with pytest.raises(ValueError, match="CIK does not match"):
        capture_recent_sec_schedules(
            tmp_path,
            "ACME",
            12345,
            "HyperOptions test@example.com",
            client=client,
        )


def test_late_first_sec_scan_skips_expired_schedule_and_continues(tmp_path):
    old_accession = "0000012345-26-000001"
    new_accession = "0000012345-26-000002"
    submissions = {
        "cik": 12345,
        "tickers": ["ACME"],
        "filings": {"recent": {
            "form": ["8-K", "8-K"],
            "accessionNumber": [old_accession, new_accession],
            "primaryDocument": ["old.htm", "new.htm"],
            "acceptanceDateTime": ["2026-09-28T12:00:00", "2026-10-31T12:00:00"],
        }},
    }

    def handler(request):
        if request.url.host == "data.sec.gov":
            return httpx.Response(200, json=submissions)
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, json={"directory": {"item": []}})
        if request.url.path.endswith("old.htm"):
            return httpx.Response(200, content=_announcement("October 1, 2026"))
        return httpx.Response(200, content=_announcement("November 5, 2026"))

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert capture_recent_sec_schedules(
        tmp_path, "ACME", 12345, "HyperOptions test@example.com",
        now=datetime(2026, 11, 1, 17, tzinfo=UTC), client=client,
    ) == 1
    current = effective_forward_schedules(
        tmp_path, "ACME", datetime(2026, 11, 1, 17, tzinfo=UTC), date(2026, 11, 30)
    )
    assert [row["accession"] for row in current] == [new_accession]


def test_sec_exhibit_991_is_bounded_and_ambiguous_index_is_ignored(tmp_path):
    submissions = {
        "cik": 12345,
        "tickers": ["ACME"],
        "filings": {
            "recent": {
                "form": ["8-K"],
                "accessionNumber": ["0000012345-26-000001"],
                "primaryDocument": ["primary.htm"],
                "acceptanceDateTime": ["2026-09-28T12:00:00"],
            }
        },
    }
    names = ["exhibit99-1.htm"]
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if request.url.host == "data.sec.gov":
            return httpx.Response(200, json=submissions)
        if request.url.path.endswith("index.json"):
            return httpx.Response(
                200, json={"directory": {"item": [{"name": name} for name in names]}}
            )
        if request.url.path.endswith("exhibit99-1.htm"):
            return httpx.Response(200, content=_announcement("November 5, 2026"))
        return httpx.Response(200, content=b"<p>Item 9.01: exhibit attached.</p>")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    now = datetime(2026, 9, 28, 17, tzinfo=UTC)
    assert (
        capture_recent_sec_schedules(
            tmp_path, "ACME", 12345, "HyperOptions test@example.com", now=now, client=client
        )
        == 1
    )
    assert len(urls) == 4 and urls[-1].endswith("exhibit99-1.htm")
    names.append("ex99_1.htm")
    urls.clear()
    assert (
        capture_recent_sec_schedules(
            tmp_path / "ambiguous",
            "ACME",
            12345,
            "HyperOptions test@example.com",
            now=now,
            client=client,
        )
        == 0
    )
    assert len(urls) == 3


def test_sec_schedule_knowledge_time_is_document_fetch_completion(tmp_path):
    accession = "0000012345-26-000001"
    submissions = {
        "cik": 12345,
        "tickers": ["ACME"],
        "filings": {"recent": {
            "form": ["8-K"],
            "accessionNumber": [accession],
            "primaryDocument": ["primary.htm"],
            "acceptanceDateTime": ["2026-09-28T12:00:00"],
        }},
    }

    def handler(request):
        if request.url.host == "data.sec.gov":
            return httpx.Response(200, json=submissions)
        if request.url.path.endswith("index.json"):
            return httpx.Response(
                200, json={"directory": {"item": [{"name": "exhibit99-1.htm"}]}}
            )
        if request.url.path.endswith("exhibit99-1.htm"):
            return httpx.Response(200, content=_announcement("November 5, 2026"))
        return httpx.Response(200, content=b"<p>Item 9.01: exhibit attached.</p>")

    started = datetime(2026, 9, 28, 17, tzinfo=UTC)
    times = iter((started, started + timedelta(minutes=1), started + timedelta(minutes=2)))
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert capture_recent_sec_schedules(
        tmp_path, "ACME", 12345, "HyperOptions test@example.com",
        client=client, observed_time=lambda: next(times),
    ) == 1
    before = started + timedelta(minutes=1, seconds=30)
    after = started + timedelta(minutes=2)
    assert known_forward_schedules(tmp_path, "ACME", before, date(2026, 11, 30)) == ()
    assert known_forward_schedules(tmp_path, "ACME", after, date(2026, 11, 30))[0][
        "retrieved_at"
    ] == after


def test_sec_capture_cli_uses_existing_data_dir_override(tmp_path, monkeypatch, capsys):
    path = Path(__file__).parents[1] / "scripts" / "capture_sec_events.py"
    spec = importlib.util.spec_from_file_location("capture_sec_events_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observed = []
    monkeypatch.setenv("STOCKSWEEPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HYPEROPTIONS_SEC_USER_AGENT", "HyperOptions test@example.com")
    monkeypatch.setattr(
        module,
        "capture_recent_sec_schedules",
        lambda *arguments: observed.append(arguments) or 2,
    )
    monkeypatch.setattr(sys, "argv", [str(path), "acme", "12345"])
    module.main()
    assert observed == [(tmp_path, "ACME", 12345, "HyperOptions test@example.com")]
    assert "forward schedules added: 2" in capsys.readouterr().out
