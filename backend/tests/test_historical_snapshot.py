"""Synthetic invariants for the offline immutable option snapshot."""

from __future__ import annotations

import gzip
import hashlib
import json
import socket
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import polars as pl
import pytest

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import price_hash
from stocksweeper.research.historical_options.artifacts import (
    atomic_directory,
    finalize_manifest,
    hash_file,
    verify_artifact,
)
from stocksweeper.research.historical_options.normalization import (
    contract_identity,
    normalize_contract,
    session_buckets,
)
from stocksweeper.research.historical_options.snapshot import freeze_snapshot, verify_snapshot


@pytest.fixture
def research_sources(tmp_path: Path):
    db = tmp_path / "archive.duckdb"
    prices = tmp_path / "forecast" / "prices"
    prices.mkdir(parents=True)
    flat = tmp_path / "flat"
    flat.mkdir()
    symbol = "O:CIFR241101C00012375"
    session = date(2024, 10, 30)
    start = datetime(2024, 10, 30, 13, tzinfo=UTC)
    midnight = datetime(2024, 10, 30, 4, tzinfo=UTC)
    timestamp = int(start.timestamp() * 1000)
    daily_timestamp = int(midnight.timestamp() * 1000)
    connection = duckdb.connect(str(db))
    connection.execute("CREATE SCHEMA massive")
    connection.execute("""
      CREATE TABLE massive.option_contracts(
        ticker VARCHAR,underlying_ticker VARCHAR,contract_type VARCHAR,exercise_style VARCHAR,
        expiration_date VARCHAR,strike_price DOUBLE,shares_per_contract BIGINT,
        _dlt_load_id VARCHAR,_dlt_id VARCHAR)
    """)
    connection.execute(
        "INSERT INTO massive.option_contracts VALUES (?,?,?,?,?,?,?,?,?)",
        [symbol, "CIFR", "call", "american", "2024-11-01", 12.375, 100, "load", "ref"],
    )
    connection.execute("""
      CREATE TABLE massive.option_hour_bars(
        option_ticker VARCHAR,underlying_ticker VARCHAR,expiration_date VARCHAR,
        strike_price BIGINT,contract_type VARCHAR,t BIGINT,bar_start TIMESTAMPTZ,
        open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume BIGINT,vwap DOUBLE,
        transactions BIGINT,adjusted BOOLEAN,_dlt_load_id VARCHAR,_dlt_id VARCHAR,
        strike_price__v_double DOUBLE)
    """)
    connection.execute(
        "INSERT INTO massive.option_hour_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            symbol,
            "CIFR",
            "2024-11-01",
            None,
            "call",
            timestamp,
            start,
            0.50,
            0.65,
            0.50,
            0.60,
            20,
            0.57,
            5,
            True,
            "load",
            "hour",
            12.375,
        ],
    )
    connection.execute("""
      CREATE TABLE massive.option_bars(
        option_ticker VARCHAR,underlying_ticker VARCHAR,expiration_date VARCHAR,
        strike_price DOUBLE,contract_type VARCHAR,t BIGINT,bar_date VARCHAR,
        open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume BIGINT,vwap DOUBLE,
        transactions BIGINT,adjusted BOOLEAN,_dlt_load_id VARCHAR,_dlt_id VARCHAR)
    """)
    connection.execute(
        "INSERT INTO massive.option_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            symbol,
            "CIFR",
            "2024-11-01",
            12.375,
            "call",
            daily_timestamp,
            session.isoformat(),
            0.50,
            0.65,
            0.50,
            0.60,
            20,
            0.57,
            5,
            True,
            "load",
            "day",
        ],
    )
    connection.execute("""
      CREATE TABLE massive.option_hour_fetches(
        option_ticker VARCHAR,results_count BIGINT,from_date VARCHAR,to_date VARCHAR,
        fetched_at TIMESTAMPTZ,_dlt_load_id VARCHAR,_dlt_id VARCHAR,daily_days BIGINT,
        hour_requests BIGINT,volume_mismatch_days BIGINT)
    """)
    connection.execute("""
      CREATE TABLE massive.option_bar_fetches(
        option_ticker VARCHAR,results_count BIGINT,from_date VARCHAR,to_date VARCHAR,
        fetched_at TIMESTAMPTZ,_dlt_load_id VARCHAR,_dlt_id VARCHAR,source VARCHAR)
    """)
    connection.execute(
        "INSERT INTO massive.option_hour_fetches VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            symbol,
            1,
            session.isoformat(),
            "2024-11-01",
            start,
            "load",
            "fetch",
            None,
            None,
            None,
        ],
    )
    connection.execute(
        "INSERT INTO massive.option_bar_fetches VALUES (?,?,?,?,?,?,?,?)",
        [symbol, 1, session.isoformat(), "2024-11-01", start, "load", "dailyfetch", "rest"],
    )
    connection.close()
    stock = pl.DataFrame(
        {
            "ts": [session],
            "open": [13.0],
            "high": [14.0],
            "low": [12.0],
            "close": [13.5],
            "volume": [100.0],
            "dividends": [0.0],
            "stock_splits": [0.0],
        },
        schema={
            "ts": pl.Date,
            **{
                name: pl.Float64
                for name in ("open", "high", "low", "close", "volume", "dividends", "stock_splits")
            },
        },
    )
    stock.write_parquet(prices / "CIFR.parquet")
    (prices / "CIFR.json").write_text(
        json.dumps(
            {
                "ticker": "CIFR",
                "source": "Yahoo Finance daily Close",
                "auto_adjust": False,
                "price_basis": "split-normalized, dividend-unadjusted",
                "through_session": session.isoformat(),
                "requested_through_session": session.isoformat(),
                "hash": price_hash(stock),
                "retrieved_at": "2026-10-04T00:00:00+00:00",
            }
        )
    )
    digest = hashlib.sha256(symbol.encode()).hexdigest()[:16]
    cache = flat / f"{session.isoformat()}.{digest}.json.gz"
    with gzip.open(cache, "wt") as stream:
        json.dump(
            [
                {
                    "ticker": symbol,
                    "bar": {
                        "t": daily_timestamp,
                        "o": 0.5,
                        "h": 0.65,
                        "l": 0.5,
                        "c": 0.6,
                        "v": 20.0,
                        "n": 5,
                        "vw": None,
                    },
                }
            ],
            stream,
        )
    return db, prices, flat


def test_fractional_strike_and_standard_terms():
    ticker, side, expiry, strike = contract_identity("O:CIFR241101C00012375")
    assert (ticker, side, expiry, str(strike)) == ("CIFR", "call", date(2024, 11, 1), "12.375")
    row = {
        "ticker": "O:CIFR241101C00012375",
        "underlying_ticker": "CIFR",
        "contract_type": "call",
        "expiration_date": "2024-11-01",
        "strike_price": 12.375,
        "shares_per_contract": 100,
        "exercise_style": "american",
    }
    calendar = SessionCalendar()
    assert normalize_contract(row, calendar)["valid"]
    assert (
        normalize_contract({**row, "shares_per_contract": 10}, calendar)["reason"]
        == "nonstandard_contract_terms"
    )
    assert not normalize_contract({**row, "ticker": "O:CIFR1241101C00012375"}, calendar)["valid"]
    assert not normalize_contract({**row, "additional_underlyings": '[{"ticker":"X"}]'}, calendar)[
        "valid"
    ]
    assert (
        normalize_contract({**row, "strike_price": 12.0}, calendar)["reason"]
        == "reference_identity_conflict"
    )


def test_dst_early_close_and_holiday_expiry():
    calendar = SessionCalendar()
    buckets = session_buckets(calendar, date(2024, 11, 1), date(2024, 11, 29))
    rows = {row["session"]: row for row in buckets.iter_rows(named=True)}
    assert rows[date(2024, 11, 1)]["opening_bucket"].hour == 13
    assert rows[date(2024, 11, 4)]["opening_bucket"].hour == 14
    assert rows[date(2024, 11, 29)]["session_close"].hour == 18
    assert date(2024, 11, 28) not in rows
    assert calendar.expiry_session(date(2024, 11, 28)) == date(2024, 11, 27)


def test_freeze_is_read_only_offline_and_reconciled(research_sources, tmp_path, monkeypatch):
    def no_network(*_args, **_kwargs):
        pytest.fail("offline freeze attempted network")

    monkeypatch.setattr(socket, "create_connection", no_network)
    db, prices, flat = research_sources
    source_hashes = {path: hash_file(path) for path in (db, *prices.iterdir(), *flat.iterdir())}
    output = tmp_path / "snapshot"
    metadata = freeze_snapshot(db, prices, flat, output)
    assert verify_snapshot(output)["canonical_hash"] == metadata["canonical_hash"]
    assert source_hashes == {path: hash_file(path) for path in source_hashes}
    days = pl.read_parquet(output / "days.parquet")
    row = days.row(0, named=True)
    assert row["strike"] == 12.375 and row["mark"] == 0.6 and row["opening_open"] == 0.5
    assert row["valid"] and not row["reconciliation_failed"]
    assert row["opening_start"].hour == 13 and row["opening_end"].hour == 14
    validation = pl.read_parquet(output / "daily_validation.parquet")
    assert set(validation["source"]) == {"rest_daily", "filtered_flat_daily"}
    assert metadata["normalization_counts"]["hourly_rows"] == 1
    with pytest.raises(FileExistsError):
        freeze_snapshot(db, prices, flat, output)


def test_missing_rest_daily_is_reconstructed_without_mutation(research_sources, tmp_path):
    db, prices, flat = research_sources
    with duckdb.connect(str(db)) as connection:
        connection.execute("DELETE FROM massive.option_bars")
    metadata = freeze_snapshot(db, prices, flat, tmp_path / "snapshot")
    assert metadata["normalization_counts"]["flat_reconstructed_days"] == 1
    assert (
        pl.read_parquet(tmp_path / "snapshot" / "days.parquet")["reconciliation_failed"][0] is False
    )
    with duckdb.connect(str(db), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM massive.option_bars").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["fractional", "duplicate", "view"])
def test_source_conflicts_fail_before_publication(research_sources, tmp_path, change):
    db, prices, flat = research_sources
    with duckdb.connect(str(db)) as connection:
        if change == "fractional":
            connection.execute("UPDATE massive.option_hour_bars SET strike_price__v_double=12.5")
        elif change == "duplicate":
            connection.execute(
                "INSERT INTO massive.option_hour_bars SELECT * FROM massive.option_hour_bars"
            )
            connection.execute("UPDATE massive.option_hour_bars SET close=0.61 WHERE rowid=1")
        else:
            connection.execute("ALTER TABLE massive.option_bars RENAME TO hidden_bars")
            connection.execute(
                "CREATE VIEW massive.option_bars AS SELECT * FROM massive.hidden_bars"
            )
    output = tmp_path / "snapshot"
    with pytest.raises(ValueError):
        freeze_snapshot(db, prices, flat, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".snapshot.*"))


def test_ohlc_reconciliation_changes_primary_eligibility(research_sources, tmp_path):
    db, prices, flat = research_sources
    with duckdb.connect(str(db)) as connection:
        connection.execute("UPDATE massive.option_bars SET close=0.59")
    freeze_snapshot(db, prices, flat, tmp_path / "snapshot")
    row = pl.read_parquet(tmp_path / "snapshot" / "days.parquet").row(0, named=True)
    assert row["valid"] and row["reconciliation_failed"]


@pytest.mark.parametrize("field", ["close", "transactions"])
def test_null_daily_records_cannot_qualify_primary(research_sources, tmp_path, field):
    db, prices, flat = research_sources
    with duckdb.connect(str(db)) as connection:
        connection.execute(f"UPDATE massive.option_bars SET {field}=NULL")
    freeze_snapshot(db, prices, flat, tmp_path / "snapshot")
    day = pl.read_parquet(tmp_path / "snapshot" / "days.parquet").row(0, named=True)
    assert day["reconciliation_failed"]
    daily = pl.read_parquet(tmp_path / "snapshot" / "daily_validation.parquet")
    assert daily.filter(pl.col("source") == "rest_daily")["valid"][0] is False


def test_expired_fetch_cutoff_is_not_stale(research_sources, tmp_path):
    db, prices, flat = research_sources
    metadata = freeze_snapshot(db, prices, flat, tmp_path / "snapshot")
    assert metadata["fetch_audit"]["stale_hour_fetch_windows"] == 0
    with duckdb.connect(str(db)) as connection:
        connection.execute("UPDATE massive.option_hour_fetches SET to_date='2024-10-29'")
    metadata = freeze_snapshot(db, prices, flat, tmp_path / "second")
    assert metadata["fetch_audit"]["stale_hour_fetch_windows"] == 1


def test_artifact_hashes_ignore_resource_measurements_but_verify_integrity(tmp_path):
    digests = []
    for name, duration in (("one", 1.0), ("two", 9.0)):
        with atomic_directory(tmp_path / name) as stage:
            (stage / "summary.json").write_text('{"score":0.2}\n')
            (stage / "resource.json").write_text(json.dumps({"duration_seconds": duration}))
            digests.append(
                finalize_manifest(stage, {"version": 1, "runtime": duration})["canonical_hash"]
            )
    assert digests[0] == digests[1]
    (tmp_path / "one" / "resource.json").write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_artifact(tmp_path / "one")


def test_safe_paths_and_failures_leave_no_completed_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    symlink = tmp_path / "link"
    symlink.symlink_to(source)
    with pytest.raises(ValueError, match="symlinks"), atomic_directory(symlink / "output"):
        pass
    with pytest.raises(RuntimeError), atomic_directory(tmp_path / "failed") as stage:
        (stage / "summary.json").write_text("{}")
        raise RuntimeError("injected export failure")
    assert not (tmp_path / "failed").exists()
    assert not list(tmp_path.glob(".failed.*"))


def test_separate_bar_and_fetch_loads_preserve_explicit_daily_source(research_sources, tmp_path):
    db, prices, flat = research_sources
    with duckdb.connect(str(db)) as connection:
        connection.execute("UPDATE massive.option_bar_fetches SET _dlt_load_id='fetch_load'")
    freeze_snapshot(db, prices, flat, tmp_path / "snapshot")
    daily = pl.read_parquet(tmp_path / "snapshot/daily_validation.parquet")
    row = daily.filter(pl.col("source") == "rest_daily").row(0, named=True)
    assert row["source_load_id"] == "load"
    assert row["source_fetch_load_ids"] == "fetch_load"


@pytest.mark.parametrize("change", ["ambiguous", "count", "window", "mixed_adjustment"])
def test_incoherent_daily_provenance_stays_unknown(research_sources, tmp_path, change):
    db, prices, flat = research_sources
    with duckdb.connect(str(db)) as connection:
        if change == "ambiguous":
            connection.execute(
                "INSERT INTO massive.option_bar_fetches SELECT * FROM massive.option_bar_fetches"
            )
        elif change == "count":
            connection.execute("UPDATE massive.option_bar_fetches SET results_count=2")
        elif change == "window":
            connection.execute("UPDATE massive.option_bar_fetches SET from_date='2024-10-31'")
        else:
            connection.execute("UPDATE massive.option_bars SET adjusted=FALSE")
    freeze_snapshot(db, prices, flat, tmp_path / "snapshot")
    daily = pl.read_parquet(tmp_path / "snapshot/daily_validation.parquet")
    assert "database_daily_unknown" in daily["source"].to_list()
