"""Adversarial end-to-end checks for immutable, offline research publication."""

from __future__ import annotations

import gzip
import errno
import importlib.util
import json
import socket
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import polars as pl
import pytest

from tests.test_historical_snapshot import research_sources as research_sources

from stocksweeper.forecast.market import ForecastPriceStore, price_hash
from stocksweeper.forecast.predictive import PredictiveForecaster
from stocksweeper.research.historical_options import artifacts, snapshot
from stocksweeper.research.historical_options.artifacts import (
    atomic_directory,
    finalize_manifest,
    hash_file,
    verify_artifact,
)
from stocksweeper.research.historical_options.runner import run_snapshot


def _forbid_network_and_refresh(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("offline research attempted network or source refresh")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(ForecastPriceStore, "update", forbidden)
    monkeypatch.setattr(PredictiveForecaster, "prepare", forbidden)


def _source_hashes(sources):
    db, prices, flat = sources
    return {path: hash_file(path) for path in (db, *prices.iterdir(), *flat.iterdir())}


def test_staging_allocation_failure_releases_lock_and_allows_retry(tmp_path, monkeypatch):
    output = tmp_path / "run"
    with monkeypatch.context() as patch:
        def full_disk(**_kwargs):
            raise OSError(errno.ENOSPC, "disk full")
        patch.setattr(artifacts.tempfile, "mkdtemp", full_disk)
        with pytest.raises(OSError, match="disk full"), atomic_directory(output):
            pytest.fail("allocation must fail before yielding")
    _assert_no_publication(output)
    with atomic_directory(output) as stage:
        (stage / "summary.json").write_text("{}")
        finalize_manifest(stage, {"version": 1})
    verify_artifact(output)


def test_competing_publication_lock_is_preserved(tmp_path):
    output = tmp_path / "run"
    lock = tmp_path / ".run.publish.lock"
    lock.write_text("other writer")
    with pytest.raises(FileExistsError), atomic_directory(output):
        pytest.fail("competing lock must prevent entry")
    assert lock.read_text() == "other writer"
    assert not output.exists()


def _assert_no_publication(output):
    assert not output.exists()
    assert not list(output.parent.glob(f".{output.name}.*"))


def test_complete_replay_is_offline_read_only_and_deterministic(
    research_sources, tmp_path, monkeypatch
):
    _forbid_network_and_refresh(monkeypatch)
    before = _source_hashes(research_sources)
    frozen = tmp_path / "snapshot"
    snapshot.freeze_snapshot(*research_sources, frozen)
    frozen_before = {path.name: hash_file(path) for path in frozen.iterdir()}
    first = run_snapshot(frozen, tmp_path / "first")
    second = run_snapshot(frozen, tmp_path / "second")

    assert first["canonical_hash"] == second["canonical_hash"]
    assert verify_artifact(tmp_path / "first")["canonical_hash"] == first["canonical_hash"]
    assert before == _source_hashes(research_sources)
    assert frozen_before == {path.name: hash_file(path) for path in frozen.iterdir()}
    summary = json.loads((tmp_path / "first" / "summary.json").read_text())
    assert set(summary["studies"]) == {"calibration", "strategies", "liquidity"}
    assert summary["studies"]["liquidity"]["archive_rows"] == 1
    assert summary["studies"]["liquidity"]["accounted_archive_rows"] == 1
    cells = pl.read_parquet(tmp_path / "first" / "forecast_cells.parquet")
    assert cells.height == 8
    assert cells["forecast_available"].to_list() == [False] * 8
    assert cells["brier"].null_count() == 8
    assert "ngboost_pooled" not in cells["model"].to_list()
    report = (tmp_path / "first" / "report.md").read_text()
    for section in ("A. Forecast", "B. Predetermined", "C. Liquidity"):
        assert section in report
    assert "unknown individual trade times" in report
    assert "not executable quotes" in report
    assert "not actual assignment" in report
    assert "not portfolio performance" in report
    analytical = (tmp_path / "first" / "summary.json").read_text()
    assert str(tmp_path) not in analytical
    assert "duration_seconds" not in analytical
    assert not list(frozen.rglob("*ngboost*"))


def test_cli_freeze_and_run_publish_complete_tiny_reports(
    research_sources, tmp_path, monkeypatch, capsys
):
    _forbid_network_and_refresh(monkeypatch)
    script = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_historical_options.py"
    # Track the CLI's intentional thread settings so they do not leak to other tests.
    for name in ("POLARS_MAX_THREADS", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                 "VECLIB_MAXIMUM_THREADS"):
        monkeypatch.setenv(name, "1")
    spec = importlib.util.spec_from_file_location("historical_options_cli_test", script)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    db, prices, flat = research_sources
    frozen, output = tmp_path / "snapshot with spaces", tmp_path / "run with spaces"
    monkeypatch.setattr(sys, "argv", [str(script), "freeze", "--source", str(db),
                                    "--prices", str(prices), "--flat-cache", str(flat),
                                    "--output", str(frozen)])
    cli.main()
    first_output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert first_output["canonical_hash"] == snapshot.verify_snapshot(frozen)["canonical_hash"]
    monkeypatch.setattr(sys, "argv", [str(script), "run", "--snapshot", str(frozen),
                                    "--output", str(output)])
    cli.main()
    last_output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert last_output["canonical_hash"] == verify_artifact(output)["canonical_hash"]
    assert (output / "report.md").is_file()


@pytest.mark.parametrize("corruption", ["data", "manifest", "extra", "missing"])
def test_corrupt_snapshot_fails_before_any_completed_run(
    research_sources, tmp_path, corruption
):
    frozen, output = tmp_path / "snapshot", tmp_path / "run"
    snapshot.freeze_snapshot(*research_sources, frozen)
    if corruption == "data":
        with (frozen / "days.parquet").open("ab") as stream:
            stream.write(b"unexpected bytes")
    elif corruption == "manifest":
        manifest = json.loads((frozen / "manifest.json").read_text())
        manifest["source_counts"]["option_hour_bars"] += 1
        (frozen / "manifest.json").write_text(json.dumps(manifest))
    elif corruption == "extra":
        (frozen / "unexpected.json").write_text("{}")
    else:
        (frozen / "stocks.parquet").unlink()
    with pytest.raises(ValueError):
        run_snapshot(frozen, output)
    _assert_no_publication(output)


def test_runner_export_failure_does_not_publish_partial_results(
    research_sources, tmp_path, monkeypatch
):
    from stocksweeper.research.historical_options import liquidity

    frozen, output = tmp_path / "snapshot", tmp_path / "run"
    snapshot.freeze_snapshot(*research_sources, frozen)

    def fail_export(_days, _stocks, _hours, stage):
        assert (stage / "forecast_cells.parquet").is_file()
        assert (stage / "outcomes.parquet").is_file()
        raise RuntimeError("injected failure after two studies")

    monkeypatch.setattr(liquidity, "run_liquidity", fail_export)
    with pytest.raises(RuntimeError, match="after two studies"):
        run_snapshot(frozen, output)
    _assert_no_publication(output)
    snapshot.verify_snapshot(frozen)


@pytest.mark.parametrize("member", ["prices", "flat", "new_flat_file"])
def test_source_change_during_capture_requires_retry_and_leaves_no_snapshot(
    research_sources, tmp_path, monkeypatch, member
):
    _db, prices, flat = research_sources
    original = snapshot._flat_validation

    def changed_after_capture(*args, **kwargs):
        result = original(*args, **kwargs)
        if member == "prices":
            manifest = prices / "CIFR.json"
            manifest.write_text(manifest.read_text() + "\n")
        elif member == "flat":
            with next(flat.iterdir()).open("ab") as stream:
                stream.write(b"changed")
        else:
            existing = next(flat.iterdir())
            new_name = existing.name.replace("2024-10-30", "2024-10-31")
            (flat / new_name).write_bytes(existing.read_bytes())
        return result

    monkeypatch.setattr(snapshot, "_flat_validation", changed_after_capture)
    output = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="source changed during capture; retry freeze"):
        snapshot.freeze_snapshot(*research_sources, output)
    _assert_no_publication(output)


@pytest.mark.parametrize("limit", ["bytes", "rows"])
def test_filtered_cache_bounds_reject_before_publication(
    research_sources, tmp_path, monkeypatch, limit
):
    _db, _prices, flat = research_sources
    if limit == "bytes":
        monkeypatch.setattr(snapshot, "_MAX_CACHE_BYTES", 8)
    else:
        monkeypatch.setattr(snapshot, "_MAX_CACHE_ROWS", 1)
        cache = next(flat.iterdir())
        with gzip.open(cache, "rt") as stream:
            rows = json.load(stream)
        with gzip.open(cache, "wt") as stream:
            json.dump(rows * 2, stream)
    output = tmp_path / "snapshot"
    with pytest.raises(ValueError, match=r"limit|oversized"):
        snapshot.freeze_snapshot(*research_sources, output)
    _assert_no_publication(output)


def test_noncooperating_destination_race_never_overwrites_new_directory(tmp_path, monkeypatch):
    output = tmp_path / "run"
    publish = artifacts._publish_exclusive

    def competing_publish(stage, destination):
        destination.mkdir()
        (destination / "owned_by_other_writer.txt").write_text("preserve me")
        publish(stage, destination)

    monkeypatch.setattr(artifacts, "_publish_exclusive", competing_publish)
    with pytest.raises(FileExistsError), atomic_directory(output) as stage:
        (stage / "summary.json").write_text("{}")
        finalize_manifest(stage, {"version": 1})
    assert [p.name for p in output.iterdir()] == ["owned_by_other_writer.txt"]
    assert (output / "owned_by_other_writer.txt").read_text() == "preserve me"
    assert not list(tmp_path.glob(".run.*"))


def test_conflicting_run_and_overlapping_input_paths_are_rejected(
    research_sources, tmp_path
):
    frozen = tmp_path / "snapshot"
    snapshot.freeze_snapshot(*research_sources, frozen)
    for output in (frozen, frozen / "nested", tmp_path):
        with pytest.raises(ValueError, match="overlap"):
            run_snapshot(frozen, output)
    output = tmp_path / "already exists"
    output.mkdir()
    sentinel = output / "existing.txt"
    sentinel.write_text("do not replace")
    with pytest.raises(FileExistsError):
        run_snapshot(frozen, output)
    assert sentinel.read_text() == "do not replace"


def test_normalized_late_bucket_cannot_become_an_opening_entry(research_sources, tmp_path):
    db, prices, _flat = research_sources
    stock = pl.read_parquet(prices / "CIFR.parquet")
    stock = pl.concat([
        stock,
        pl.DataFrame({
            "ts": [date(2024, 10, 31), date(2024, 11, 1)],
            "open": [13.1, 13.2], "high": [14.0, 14.0], "low": [12.0, 12.0],
            "close": [13.2, 13.3], "volume": [100.0, 100.0],
            "dividends": [0.0, 0.0], "stock_splits": [0.0, 0.0],
        }),
    ])
    stock.write_parquet(prices / "CIFR.parquet")
    metadata_path = prices / "CIFR.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.update(hash=price_hash(stock), through_session="2024-11-01",
                    requested_through_session="2024-11-01")
    metadata_path.write_text(json.dumps(metadata))
    later_bucket = datetime(2024, 10, 31, 14, tzinfo=UTC)  # 10:00 ET, after 09:30 open.
    with duckdb.connect(str(db)) as connection:
        connection.execute(
            "INSERT INTO massive.option_hour_bars SELECT option_ticker,underlying_ticker,"
            "expiration_date,strike_price,contract_type,?, ?,open,high,low,close,volume,"
            "vwap,transactions,adjusted,_dlt_load_id,'late-entry',strike_price__v_double "
            "FROM massive.option_hour_bars LIMIT 1",
            [int(later_bucket.timestamp() * 1000), later_bucket],
        )
    frozen, output = tmp_path / "snapshot", tmp_path / "run"
    snapshot.freeze_snapshot(*research_sources, frozen)
    entry_day = pl.read_parquet(frozen / "days.parquet").filter(
        pl.col("session") == date(2024, 10, 31)
    )
    assert entry_day["valid"].to_list() == [True]
    assert entry_day["mark"].to_list() == [0.6]
    assert entry_day["opening_open"].to_list() == [None]
    run_snapshot(frozen, output)
    selections = pl.read_parquet(output / "selections.parquet").filter(
        (pl.col("origin") == date(2024, 10, 30)) & (pl.col("policy") == "maximum_apr")
    )
    assert selections.height > 0
    assert selections["contract"].unique().to_list() == ["O:CIFR241101C00012375"]
    assert selections["status"].unique().to_list() == ["no_entry"]
    assert selections["option_entry"].null_count() == selections.height
    outcomes = pl.read_parquet(output / "outcomes.parquet").filter(
        pl.col("selection_id").is_in(selections["selection_id"].to_list())
    )
    assert outcomes["return"].null_count() == outcomes.height
    assert outcomes["status"].unique().to_list() == ["no_entry"]


def test_missing_source_database_is_not_created(research_sources, tmp_path):
    _db, prices, flat = research_sources
    absent = tmp_path / "absent.duckdb"
    output = tmp_path / "snapshot"
    with pytest.raises(FileNotFoundError):
        snapshot.freeze_snapshot(absent, prices, flat, output)
    assert not absent.exists()
    _assert_no_publication(output)


def test_symlink_artifact_file_cannot_pass_verification(tmp_path):
    output = tmp_path / "run"
    with atomic_directory(output) as stage:
        (stage / "summary.json").write_text("{}")
        finalize_manifest(stage, {"version": 1})
    owned = tmp_path / "other.json"
    owned.write_text("{}")
    (output / "summary.json").unlink()
    (output / "summary.json").symlink_to(owned)
    with pytest.raises(ValueError, match="unsafe"):
        verify_artifact(output)
