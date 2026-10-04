"""Freeze collected data without application database helpers or network calls.

The source connection is read-only, with a single consistent transaction and
external access disabled. Only fixed canonical Massive base tables are read.
Arrow batches leave that connection through ParquetWriter, never SQL COPY.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.metadata
import json
import math
import platform
import re
import resource
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import polars as pl
import pyarrow.parquet as pq

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import ForecastPriceStore, clean_completed, price_hash
from stocksweeper.research.historical_options import SNAPSHOT_VERSION
from stocksweeper.research.historical_options.artifacts import (
    atomic_directory,
    canonical_hash,
    finalize_manifest,
    hash_file,
    safe_path,
    verify_artifact,
)
from stocksweeper.research.historical_options.normalization import (
    EASTERN,
    IDENTITY_BOUNDARIES,
    normalize_contract,
    session_buckets,
)

_TABLES = (
    "option_contracts", "option_hour_bars", "option_bars",
    "option_hour_fetches", "option_bar_fetches",
)
_CACHE_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})\.([0-9a-f]{16})\.json\.gz\Z")
_MAX_CACHE_BYTES = 64 * 1024 * 1024
_MAX_CACHE_ROWS = 100_000
_BATCH = 65_536
_DB_CONFIG = {
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "threads": 1,
}
_VALIDATION_SCHEMA = {
    "ticker": pl.String, "contract": pl.String, "session": pl.Date,
    "source": pl.String, "source_t": pl.Int64,
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Float64, "transactions": pl.Int64, "vwap": pl.Float64,
    "adjusted": pl.Boolean, "valid": pl.Boolean, "reason": pl.String,
    "source_load_id": pl.String, "source_row_id": pl.String,
    "source_file": pl.String, "source_fetch_kind": pl.String,
    "source_fetch_load_ids": pl.String,
}


class _NoFetch:
    def fetch(self, *_args, **_kwargs):
        raise RuntimeError("historical research cannot fetch prices")


def environment_metadata() -> dict:
    versions = {}
    for name in (
        "duckdb", "polars", "pyarrow", "numpy", "exchange-calendars", "scipy",
        "arch", "statsmodels", "regimelib",
    ):
        versions[name] = importlib.metadata.version(name)
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL
        ).strip())
        # Bind uncommitted implementation bytes without disclosing paths/content.
        diff = subprocess.check_output(["git", "diff", "HEAD", "--", "backend", "docs"])
        code_hash = hashlib.sha256(diff).hexdigest() if dirty else None
    except (OSError, subprocess.SubprocessError):
        revision, dirty, code_hash = "unavailable", None, None
    package = Path(__file__).resolve().parents[2]
    source_hashes = {
        str(path.relative_to(package)): hash_file(path)
        for path in sorted(package.rglob("*.py"))
    }
    return {
        "python": platform.python_version(), "dependencies": versions,
        "code_revision": revision, "code_dirty": dirty, "code_diff_hash": code_hash,
        "code_content_hash": canonical_hash(source_hashes),
    }


def _capture_prices(prices_dir: Path, tickers: list[str]) -> tuple[pl.DataFrame, dict, dict]:
    store = ForecastPriceStore(prices_dir.parent.parent, _NoFetch())
    # Explicit source directories retain the exact existing store validation.
    # Neither construction nor read performs refreshes or writes.
    store.root = prices_dir
    calendar = SessionCalendar()
    frames, metadata, fingerprints = [], {}, {}
    for ticker in tickers:
        path = store.path(ticker)
        for member in (path, path.with_suffix(".json")):
            safe_path(member, must_exist=True)
            fingerprints[member] = hash_file(member)
        frame = store.read(ticker)
        if frame is None or frame.is_empty():
            raise ValueError(f"verified stock cache unavailable: {ticker}")
        if clean_completed(frame, frame["ts"][-1], calendar).height != frame.height:
            raise ValueError(f"invalid completed stock history: {ticker}")
        source = json.loads(path.with_suffix(".json").read_text())
        if source.get("hash") != price_hash(frame):
            raise ValueError("stock cache changed during capture; retry freeze")
        retrieved = datetime.fromisoformat(source["retrieved_at"])
        if retrieved.tzinfo is None:
            raise ValueError("stock cache retrieval timestamp must be aware")
        boundary = IDENTITY_BOUNDARIES.get(ticker, frame["ts"][0])
        qualified = frame.filter(pl.col("ts") >= boundary)
        if qualified.is_empty():
            raise ValueError(f"no verified company-identity stock history: {ticker}")
        frames.append(qualified.with_columns(pl.lit(ticker).alias("ticker")))
        metadata[ticker] = {
            "source": source["source"], "source_retrieved_at": retrieved.isoformat(),
            "source_hash": source["hash"], "frozen_price_hash": price_hash(qualified),
            "price_basis": source["price_basis"], "identity_boundary": boundary.isoformat(),
            "first_session": qualified["ts"][0].isoformat(),
            "through_session": qualified["ts"][-1].isoformat(), "rows": qualified.height,
            "source_rows": frame.height, "identity_excluded_rows": frame.height - qualified.height,
            "minimum_close": qualified["close"].min(), "maximum_close": qualified["close"].max(),
            "auto_adjust": False,
        }
    return pl.concat(frames).sort(["ticker", "ts"]), metadata, fingerprints


def _stream(connection: duckdb.DuckDBPyConnection, query: str, output: Path) -> int:
    reader = connection.execute(query).fetch_record_batch(_BATCH)
    count = 0
    with pq.ParquetWriter(output, reader.schema, compression="zstd") as writer:
        for batch in reader:
            writer.write_batch(batch)
            count += batch.num_rows
    return count


def _schemas(connection: duckdb.DuckDBPyConnection) -> dict:
    tables = dict(connection.execute(
        "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema='massive'"
    ).fetchall())
    result = {}
    for name in _TABLES:
        if tables.get(name) != "BASE TABLE":
            raise ValueError(f"canonical Massive base table unavailable: {name}")
        result[name] = connection.execute(f"DESCRIBE massive.{name}").fetchall()
    return result


def _column_names(schemas: dict, table: str) -> set[str]:
    return {row[0] for row in schemas[table]}


def _contracts(connection, schemas: dict, calendar: SessionCalendar) -> tuple[pl.DataFrame, dict]:
    columns = _column_names(schemas, "option_contracts")
    additional = "additional_underlyings" if "additional_underlyings" in columns else "NULL"
    raw = connection.execute(
        "SELECT ticker, underlying_ticker, contract_type, exercise_style, expiration_date, "
        "strike_price, shares_per_contract, _dlt_load_id, _dlt_id, "
        f"{additional} AS additional_underlyings FROM massive.option_contracts "
        "ORDER BY ticker, _dlt_id"
    ).fetch_arrow_table()
    rows = pl.from_arrow(raw).to_dicts()
    seen, normalized, duplicates = {}, [], []
    for row in rows:
        identity = canonical_hash({
            key: (
                {"invalid_numeric": str(value)}
                if isinstance(value, float) and not math.isfinite(value) else value
            )
            for key, value in row.items() if not key.startswith("_dlt")
        })
        symbol = row["ticker"]
        if symbol in seen:
            if seen[symbol] != identity:
                raise ValueError("conflicting duplicate contract references")
            duplicates.append(symbol)
            continue
        seen[symbol] = identity
        normalized.append(normalize_contract(row, calendar))
    frame = pl.DataFrame(normalized, schema={
        "contract": pl.String, "ticker": pl.String, "side": pl.String,
        "strike": pl.Float64, "expiry": pl.Date, "expiry_session": pl.Date,
        "shares_per_contract": pl.Int64, "exercise_style": pl.String,
        "valid": pl.Boolean, "reason": pl.String,
        "source_load_id": pl.String, "source_row_id": pl.String,
    }).sort("contract")
    if frame.is_empty():
        raise ValueError("empty option reference archive")
    return frame, {"raw": len(rows), "normalized": frame.height, "identical_duplicates": duplicates}


def _hours(connection, schemas: dict, output: Path) -> tuple[int, int]:
    fields = _column_names(schemas, "option_hour_bars")
    strike = (
        "COALESCE(h.strike_price__v_double, CAST(h.strike_price AS DOUBLE))"
        if "strike_price__v_double" in fields else "CAST(h.strike_price AS DOUBLE)"
    )
    conflict = connection.execute(
        "SELECT COUNT(*) FROM massive.option_hour_bars h JOIN normalized_contracts c "
        f"ON h.option_ticker=c.contract WHERE c.valid AND ({strike} != c.strike "
        "OR h.underlying_ticker != c.ticker OR h.contract_type != c.side "
        "OR TRY_CAST(h.expiration_date AS DATE) != c.expiry)"
    ).fetchone()[0]
    if conflict:
        raise ValueError("hourly/reference encoded identity or fractional-strike conflict")
    if "strike_price__v_double" in fields:
        variant_conflicts = connection.execute(
            "SELECT COUNT(*) FROM massive.option_hour_bars WHERE strike_price IS NOT NULL "
            "AND strike_price__v_double IS NOT NULL "
            "AND CAST(strike_price AS DOUBLE)!=strike_price__v_double"
        ).fetchone()[0]
        if variant_conflicts:
            raise ValueError("conflicting hourly integer/fractional strike variants")
    duplicate_keys = connection.execute(
        "SELECT option_ticker AS contract,t AS source_t,COUNT(*) AS duplicates, "
        "COUNT(DISTINCT struct_pack(o:=open,h:=high,l:=low,c:=close,v:=volume,n:=transactions,"
        "a:=adjusted,s:=bar_start)) AS variants FROM massive.option_hour_bars "
        "GROUP BY option_ticker,t HAVING COUNT(*)>1"
    ).fetch_arrow_table()
    if any(value > 1 for value in duplicate_keys.column("variants").to_pylist()):
        raise ValueError("conflicting duplicate hourly bars")
    connection.register("duplicate_hours", duplicate_keys)
    query = f"""
    WITH base AS (
      SELECT COALESCE(c.ticker,h.underlying_ticker) AS ticker,h.option_ticker AS contract,
        CAST(timezone('America/New_York',h.bar_start) AS DATE) AS session,
        EXTRACT(hour FROM timezone('America/New_York',h.bar_start))::INTEGER AS hour,
        h.bar_start AS start,h.bar_start + INTERVAL '1 hour' AS "end",
        h.bar_start AS bucket_start,h.bar_start + INTERVAL '1 hour' AS bucket_end,
        h.t AS source_t,h.bar_start AS source_bar_start,
        c.expiry,c.expiry_session,c.side,{strike} AS strike,
        h.open,h.high,h.low,h.close,h.volume,h.transactions,h.vwap,h.adjusted,
        h._dlt_load_id AS source_load_id,h._dlt_id AS source_row_id,
        s.session_open,s.session_close,s.opening_bucket,
        COALESCE(s.session IS NOT NULL AND h.bar_start<s.session_close
          AND h.bar_start + INTERVAL '1 hour'>s.session_open,FALSE) AS regular_session,
        CASE WHEN c.contract IS NULL THEN 'missing_reference'
          WHEN NOT c.valid THEN c.reason
          WHEN {strike} IS NULL OR h.underlying_ticker IS NULL OR h.contract_type IS NULL
            OR TRY_CAST(h.expiration_date AS DATE) IS NULL THEN 'missing_hour_identity'
          WHEN h.bar_start IS NULL OR h.t IS NULL
            OR epoch_ms(h.bar_start)!=h.t THEN 'timestamp_conflict'
          WHEN d.duplicates IS NOT NULL THEN 'duplicate_hour'
          WHEN h.open IS NULL OR h.high IS NULL OR h.low IS NULL OR h.close IS NULL
            OR NOT isfinite(h.open) OR NOT isfinite(h.high) OR NOT isfinite(h.low)
            OR NOT isfinite(h.close) OR h.open<=0 OR h.low<=0 OR h.close<=0
            OR h.high<h.low OR h.open<h.low OR h.open>h.high
            OR h.close<h.low OR h.close>h.high THEN 'invalid_ohlc'
          WHEN h.volume IS NULL OR h.transactions IS NULL OR h.volume<=0
            OR h.transactions<=0 THEN 'invalid_activity'
          WHEN h.adjusted IS NULL THEN 'unknown_adjustment'
          WHEN CAST(timezone('America/New_York',h.bar_start) AS DATE)>c.expiry_session
            THEN 'post_expiry_bar'
          ELSE NULL END AS reason
      FROM massive.option_hour_bars h
      LEFT JOIN normalized_contracts c ON c.contract=h.option_ticker
      LEFT JOIN calendar_sessions s
        ON s.session=CAST(timezone('America/New_York',h.bar_start) AS DATE)
      LEFT JOIN duplicate_hours d ON d.contract=h.option_ticker AND d.source_t=h.t
    ) SELECT *,reason IS NULL AS valid FROM base ORDER BY contract,source_t,source_row_id
    """
    return _stream(connection, query, output), duplicate_keys.num_rows


def _rest_validation(connection, schemas: dict, output: Path) -> int:
    source = (
        "source" if "source" in _column_names(schemas, "option_bar_fetches")
        else "NULL::VARCHAR"
    )
    query = f"""
    WITH fetch_source AS (
      SELECT option_ticker,COUNT(*) AS fetch_rows,
        CASE WHEN COUNT(*)=1 THEN MAX({source}) ELSE NULL END AS source,
        MAX(results_count) AS results_count,
        TRY_CAST(MAX(from_date) AS DATE) AS from_date,
        TRY_CAST(MAX(to_date) AS DATE) AS to_date,
        string_agg(DISTINCT _dlt_load_id, ',' ORDER BY _dlt_load_id) AS fetch_load_ids
      FROM massive.option_bar_fetches GROUP BY option_ticker
    ), daily_coherence AS (
      SELECT option_ticker,COUNT(*) AS daily_rows,
        COUNT(DISTINCT _dlt_load_id) AS bar_loads,COUNT(_dlt_load_id) AS load_rows,
        COUNT(TRY_CAST(bar_date AS DATE)) AS dated_rows,
        MIN(TRY_CAST(bar_date AS DATE)) AS first_date,
        MAX(TRY_CAST(bar_date AS DATE)) AS last_date,
        COUNT(adjusted) AS adjustment_rows,
        bool_and(adjusted) AS all_adjusted,bool_and(NOT adjusted) AS all_unadjusted
      FROM massive.option_bars GROUP BY option_ticker
    ), qualified_source AS (
      SELECT f.*,
        CASE WHEN f.fetch_rows=1 AND b.daily_rows=f.results_count
          AND b.bar_loads=1 AND b.load_rows=b.daily_rows AND b.dated_rows=b.daily_rows
          AND b.adjustment_rows=b.daily_rows AND b.first_date>=f.from_date
          AND b.last_date<=f.to_date
          AND ((f.source='rest' AND b.all_adjusted)
               OR (f.source='flatfile' AND b.all_unadjusted))
          THEN f.source ELSE NULL END AS qualified_kind
      FROM fetch_source f LEFT JOIN daily_coherence b USING(option_ticker)
    )
    SELECT COALESCE(c.ticker,d.underlying_ticker) AS ticker,d.option_ticker AS contract,
      TRY_CAST(d.bar_date AS DATE) AS session,
      CASE WHEN f.qualified_kind='rest' THEN 'rest_daily'
        WHEN f.qualified_kind='flatfile' THEN 'database_flat_daily'
        ELSE 'database_daily_unknown' END AS source,d.t AS source_t,
      d.open,d.high,d.low,d.close,CAST(d.volume AS DOUBLE) AS volume,d.transactions,
      d.vwap,d.adjusted,
      COALESCE(c.valid AND d.underlying_ticker=c.ticker AND d.contract_type=c.side
        AND d.strike_price=c.strike AND TRY_CAST(d.expiration_date AS DATE)=c.expiry
        AND TRY_CAST(d.bar_date AS DATE) IS NOT NULL
        AND TRY_CAST(d.bar_date AS DATE)=
          CAST(timezone('America/New_York',to_timestamp(d.t/1000.0)) AS DATE)
        AND d.open>0 AND d.close>0 AND d.low>0 AND d.high>=d.low
        AND d.open BETWEEN d.low AND d.high AND d.close BETWEEN d.low AND d.high
        AND isfinite(d.open) AND isfinite(d.close) AND isfinite(d.high) AND isfinite(d.low)
        AND d.volume>0 AND d.transactions>0 AND d.adjusted IS NOT NULL,FALSE) AS valid,
      CASE WHEN c.contract IS NULL THEN 'missing_reference'
        WHEN NOT c.valid THEN c.reason
        WHEN d.underlying_ticker IS NULL OR d.contract_type IS NULL OR d.strike_price IS NULL
          OR TRY_CAST(d.expiration_date AS DATE) IS NULL THEN 'missing_daily_identity'
        WHEN d.underlying_ticker!=c.ticker OR d.contract_type!=c.side OR d.strike_price!=c.strike
          OR TRY_CAST(d.expiration_date AS DATE)!=c.expiry THEN 'daily_identity_conflict'
        WHEN TRY_CAST(d.bar_date AS DATE) IS NULL THEN 'invalid_daily_date'
        WHEN TRY_CAST(d.bar_date AS DATE)!=
          CAST(timezone('America/New_York',to_timestamp(d.t/1000.0)) AS DATE)
          THEN 'timestamp_conflict'
        WHEN d.open IS NULL OR d.close IS NULL OR d.low IS NULL OR d.high IS NULL
          OR d.open<=0 OR d.close<=0 OR d.low<=0 OR d.high<d.low
          OR d.open NOT BETWEEN d.low AND d.high OR d.close NOT BETWEEN d.low AND d.high
          OR NOT isfinite(d.open) OR NOT isfinite(d.close)
          OR NOT isfinite(d.high) OR NOT isfinite(d.low) THEN 'invalid_ohlc'
        WHEN d.volume IS NULL OR d.transactions IS NULL OR d.volume<=0 OR d.transactions<=0
          THEN 'invalid_activity'
        WHEN d.adjusted IS NULL THEN 'unknown_adjustment' ELSE NULL END AS reason,
      d._dlt_load_id AS source_load_id,d._dlt_id AS source_row_id,
      NULL::VARCHAR AS source_file,f.source AS source_fetch_kind,
      f.fetch_load_ids AS source_fetch_load_ids
    FROM massive.option_bars d LEFT JOIN normalized_contracts c ON c.contract=d.option_ticker
    LEFT JOIN qualified_source f ON f.option_ticker=d.option_ticker
    ORDER BY contract,session,source_t,source_row_id
    """
    return _stream(connection, query, output)


def _flat_validation(flat_cache: Path, contracts: pl.DataFrame, output: Path) -> tuple[int, dict]:
    catalogue = {row["contract"]: row for row in contracts.iter_rows(named=True)}
    filtered_hash = hashlib.sha256("\n".join(sorted(catalogue)).encode()).hexdigest()[:16]
    files = sorted(flat_cache.glob("*.json.gz"))
    if not files:
        raise ValueError("filtered local daily flat-file cache is empty")
    count, fingerprints = 0, {}
    schema = pl.DataFrame(schema=_VALIDATION_SCHEMA).to_arrow().schema
    with pq.ParquetWriter(output, schema, compression="zstd") as writer:
        for path in files:
            safe_path(path, must_exist=True)
            match = _CACHE_NAME.fullmatch(path.name)
            if match is None or match[2] != filtered_hash:
                raise ValueError("flat-file cache catalogue filter differs from frozen reference")
            session = date.fromisoformat(match[1])
            fingerprints[path] = hash_file(path)
            with gzip.open(path, "rb") as stream:
                raw = stream.read(_MAX_CACHE_BYTES + 1)
            if len(raw) > _MAX_CACHE_BYTES:
                raise ValueError("flat-file cache exceeds bounded decompression limit")
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) > _MAX_CACHE_ROWS:
                raise ValueError("invalid or oversized flat-file cache")
            normalized, seen = [], set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"ticker", "bar"}:
                    raise ValueError("invalid flat-file cache row")
                contract, bar = row["ticker"], row["bar"]
                if contract not in catalogue or not isinstance(bar, dict):
                    raise ValueError("flat-file cache has unknown contract or malformed bar")
                required = {"t", "o", "h", "l", "c", "v", "n"}
                if not required.issubset(bar):
                    raise ValueError("flat-file cache is missing fields")
                key = (contract, bar["t"])
                if key in seen:
                    raise ValueError("duplicate flat-file cache bar")
                seen.add(key)
                c = catalogue[contract]
                try:
                    stamped = (
                        datetime.fromtimestamp(bar["t"] / 1000, UTC).astimezone(EASTERN).date()
                    )
                    valid = (
                        c["valid"] and stamped == session
                        and all(
                            math.isfinite(float(bar[name]))
                            for name in ("o", "h", "l", "c", "v", "n")
                        )
                        and bar["o"] > 0 and bar["c"] > 0 and bar["l"] > 0
                        and bar["h"] >= bar["l"] and bar["l"] <= bar["o"] <= bar["h"]
                        and bar["l"] <= bar["c"] <= bar["h"] and bar["v"] > 0 and bar["n"] > 0
                    )
                except (TypeError, ValueError, OverflowError):
                    valid = False
                normalized.append({
                    "ticker": c["ticker"], "contract": contract, "session": session,
                    "source": "filtered_flat_daily", "source_t": bar["t"],
                    "open": bar["o"], "high": bar["h"], "low": bar["l"], "close": bar["c"],
                    "volume": bar["v"], "transactions": bar["n"], "vwap": bar.get("vw"),
                    "adjusted": False, "valid": valid,
                    "reason": None if valid else "invalid_flat_daily",
                    "source_load_id": None, "source_row_id": None, "source_file": path.name,
                    "source_fetch_kind": "filtered_flat_cache", "source_fetch_load_ids": None,
                })
            frame = pl.DataFrame(normalized, schema=_VALIDATION_SCHEMA).sort(
                ["contract", "source_t"]
            )
            writer.write_table(frame.to_arrow())
            count += frame.height
    return count, fingerprints


def _fetches(connection, schemas: dict, output: Path) -> int:
    hour = _column_names(schemas, "option_hour_fetches")
    day = _column_names(schemas, "option_bar_fetches")
    daily_days = "daily_days" if "daily_days" in hour else "NULL::BIGINT"
    requests = "hour_requests" if "hour_requests" in hour else "NULL::BIGINT"
    mismatch = "volume_mismatch_days" if "volume_mismatch_days" in hour else "NULL::BIGINT"
    source = "source" if "source" in day else "NULL::VARCHAR"
    query = f"""
    SELECT 'hour' AS kind, option_ticker AS contract, results_count,
      TRY_CAST(from_date AS DATE) AS from_date,TRY_CAST(to_date AS DATE) AS to_date,
      fetched_at,{daily_days} AS daily_days,{requests} AS hour_requests,
      {mismatch} AS volume_mismatch_days,NULL::VARCHAR AS source,
      _dlt_load_id AS source_load_id,_dlt_id AS source_row_id
    FROM massive.option_hour_fetches
    UNION ALL
    SELECT 'day' AS kind,option_ticker AS contract,results_count,
      TRY_CAST(from_date AS DATE) AS from_date,TRY_CAST(to_date AS DATE) AS to_date,
      fetched_at,NULL::BIGINT,NULL::BIGINT,NULL::BIGINT,{source} AS source,
      _dlt_load_id AS source_load_id,_dlt_id AS source_row_id
    FROM massive.option_bar_fetches ORDER BY contract,kind,source_row_id
    """
    return _stream(connection, query, output)


def _derive_days(stage: Path) -> dict:
    hours = pl.scan_parquet(stage / "hours.parquet")
    usable = pl.col("regular_session") & pl.col("valid")
    opening = usable & (pl.col("start") == pl.col("opening_bucket"))
    days = hours.group_by(["ticker", "contract", "session"]).agg(
        pl.col("expiry").first(), pl.col("expiry_session").first(), pl.col("side").first(),
        pl.col("strike").first(),
        pl.col("open").filter(usable).first().alias("hourly_open"),
        pl.col("high").filter(usable).max().alias("hourly_high"),
        pl.col("low").filter(usable).min().alias("hourly_low"),
        pl.col("close").filter(usable).last().alias("mark"),
        pl.col("start").filter(usable).last().alias("closing_start"),
        pl.col("end").filter(usable).last().alias("closing_end"),
        pl.col("volume").filter(usable).sum().alias("volume"),
        pl.col("transactions").filter(usable).sum().alias("transactions"),
        pl.col("open").filter(opening).first().alias("opening_open"),
        pl.col("start").filter(opening).first().alias("opening_start"),
        pl.col("end").filter(opening).first().alias("opening_end"),
        pl.col("regular_session").sum().alias("regular_hours"),
        pl.len().alias("archive_hours"),
        pl.col("adjusted").filter(usable).first(),
        (~pl.col("valid") & pl.col("regular_session")).any().alias("invalid_regular_bar"),
        pl.col("reason").drop_nulls().first().alias("bar_reason"),
    ).with_columns((pl.col("expiry") - pl.col("session")).dt.total_days().alias("calendar_dte"))
    # Database and filtered-cache comparisons remain distinct: adjustment bases are not
    # silently blended. Aggregate duplicates are flagged, never multiplied by joins.
    validation = pl.scan_parquet(stage / "daily_validation.parquet")
    for source, prefix in (("database", "database"), ("filtered_flat_daily", "flat")):
        selected_source = (
            pl.col("source") != "filtered_flat_daily"
            if source == "database" else pl.col("source") == source
        )
        grouped = validation.filter(selected_source).group_by(
            ["contract", "session"]
        ).agg(
            pl.len().alias(f"{prefix}_rows"),
            pl.col("volume").first().alias(f"{prefix}_volume"),
            pl.col("transactions").first().alias(f"{prefix}_transactions"),
            pl.col("open").first().alias(f"{prefix}_open"),
            pl.col("close").first().alias(f"{prefix}_close"),
            pl.col("high").first().alias(f"{prefix}_high"),
            pl.col("low").first().alias(f"{prefix}_low"),
            pl.col("valid").fill_null(False).all().alias(f"{prefix}_valid"),
            pl.col("adjusted").first().alias(f"{prefix}_adjusted"),
        )
        days = days.join(grouped, on=["contract", "session"], how="left")
    days = days.with_columns(
        (
            (pl.col("database_rows").fill_null(0) > 1)
            | (pl.col("flat_rows").fill_null(0) > 1)
            | (pl.col("database_valid") == False).fill_null(False)  # noqa: E712
            | (pl.col("flat_valid") == False).fill_null(False)  # noqa: E712
            | (
                pl.col("database_volume").is_not_null()
                & (pl.col("database_volume") != pl.col("volume"))
            )
            | (
                pl.col("database_transactions").is_not_null()
                & (pl.col("database_transactions") != pl.col("transactions"))
            )
            | (
                (pl.col("database_adjusted") == pl.col("adjusted"))
                & (
                    ((pl.col("database_open") - pl.col("hourly_open")).abs() > 1e-9)
                    | ((pl.col("database_close") - pl.col("mark")).abs() > 1e-9)
                    | ((pl.col("database_high") - pl.col("hourly_high")).abs() > 1e-9)
                    | ((pl.col("database_low") - pl.col("hourly_low")).abs() > 1e-9)
                )
            ).fill_null(False)
            | (
                pl.col("database_volume").is_null() & pl.col("flat_volume").is_not_null()
                & (pl.col("flat_volume") != pl.col("volume"))
            )
            | (
                pl.col("database_volume").is_null() & pl.col("flat_transactions").is_not_null()
                & (pl.col("flat_transactions") != pl.col("transactions"))
            )
            | (
                pl.col("database_volume").is_null()
                & (pl.col("flat_adjusted") == pl.col("adjusted"))
                & (
                    ((pl.col("flat_open") - pl.col("hourly_open")).abs() > 1e-9)
                    | ((pl.col("flat_close") - pl.col("mark")).abs() > 1e-9)
                    | ((pl.col("flat_high") - pl.col("hourly_high")).abs() > 1e-9)
                    | ((pl.col("flat_low") - pl.col("hourly_low")).abs() > 1e-9)
                )
            ).fill_null(False)
        ).alias("reconciliation_failed"),
        (
            pl.col("database_rows").is_null() & pl.col("flat_rows").is_null()
        ).alias("validation_missing"),
        (
            pl.col("database_rows").is_null() & pl.col("flat_rows").is_not_null()
        ).alias("flat_reconstructed_validation"),
        (
            pl.col("database_volume").is_not_null() & pl.col("flat_volume").is_not_null()
            & (pl.col("database_volume") != pl.col("flat_volume"))
        ).alias("daily_source_volume_conflict"),
    ).with_columns(
        (
            pl.col("mark").is_not_null() & (pl.col("mark") > 0)
            & (pl.col("volume") > 0) & (pl.col("transactions") > 0)
            & ~pl.col("invalid_regular_bar") & (pl.col("session") <= pl.col("expiry_session"))
        ).fill_null(False).alias("valid"),
    ).with_columns(
        pl.when(pl.col("invalid_regular_bar")).then(pl.col("bar_reason"))
        .when(pl.col("regular_hours") == 0).then(pl.lit("no_regular_session_bar"))
        .when(pl.col("mark").is_null()).then(pl.lit("no_valid_regular_mark"))
        .when(pl.col("session") > pl.col("expiry_session")).then(pl.lit("post_expiry_bar"))
        .otherwise(None).alias("reason"),
    ).sort(["ticker", "session", "contract"])
    days.sink_parquet(stage / "days.parquet", compression="zstd")
    frame = pl.scan_parquet(stage / "days.parquet")
    return frame.select(
        pl.len().alias("observed_contract_days"),
        pl.col("valid").sum().alias("valid_contract_days"),
        pl.col("reconciliation_failed").sum().alias("reconciliation_failed_days"),
        pl.col("validation_missing").sum().alias("validation_missing_days"),
        pl.col("flat_reconstructed_validation").sum().alias("flat_reconstructed_days"),
        pl.col("daily_source_volume_conflict").sum().alias("daily_source_volume_conflict_days"),
    ).collect(engine="streaming").row(0, named=True)


def _exclusions(stage: Path, contract_duplicates: list[str]) -> dict:
    contract = pl.scan_parquet(stage / "contracts.parquet").filter(~pl.col("valid")).select(
        "ticker", "contract", pl.lit(None, dtype=pl.Date).alias("session"),
        pl.lit("reference").alias("grain"), "reason", pl.lit(1).alias("count"),
    )
    hour = pl.scan_parquet(stage / "hours.parquet").filter(
        ~pl.col("valid") | ~pl.col("regular_session")
    ).group_by(["ticker", "contract", "session", "reason", "regular_session"]).agg(
        pl.len().alias("count")
    ).select(
        "ticker", "contract", "session", pl.lit("hour").alias("grain"),
        pl.when(pl.col("reason").is_not_null()).then(pl.col("reason"))
        .otherwise(pl.lit("outside_regular_session")).alias("reason"), "count",
    )
    day = pl.scan_parquet(stage / "days.parquet").filter(
        ~pl.col("valid") | pl.col("reconciliation_failed") | pl.col("validation_missing")
    ).select(
        "ticker", "contract", "session", pl.lit("contract_day").alias("grain"),
        pl.when(~pl.col("valid")).then(pl.col("reason"))
        .when(pl.col("reconciliation_failed")).then(pl.lit("daily_hour_reconciliation"))
        .otherwise(pl.lit("daily_validation_missing")).alias("reason"),
        pl.lit(1).alias("count"),
    )
    sources = [contract, hour, day]
    if contract_duplicates:
        sources.append(pl.DataFrame({
            "ticker": [None] * len(contract_duplicates), "contract": contract_duplicates,
            "session": [None] * len(contract_duplicates),
            "grain": ["reference"] * len(contract_duplicates),
            "reason": ["identical_duplicate_reference"] * len(contract_duplicates),
            "count": [1] * len(contract_duplicates),
        }, schema={
            "ticker": pl.String, "contract": pl.String, "session": pl.Date,
            "grain": pl.String, "reason": pl.String, "count": pl.Int64,
        }).lazy())
    pl.concat(sources, how="vertical_relaxed").sort(
        ["grain", "ticker", "contract", "session", "reason"]
    ).sink_parquet(stage / "exclusions.parquet", compression="zstd")
    return {
        row["reason"]: row["count"]
        for row in pl.scan_parquet(stage / "exclusions.parquet").group_by("reason").agg(
            pl.col("count").sum()
        ).collect(engine="streaming").iter_rows(named=True)
    }


def freeze_snapshot(db: Path, prices_dir: Path, flat_cache: Path, output: Path) -> dict:
    started = time.perf_counter()
    db = safe_path(db, must_exist=True)
    prices_dir = safe_path(prices_dir, must_exist=True)
    flat_cache = safe_path(flat_cache, must_exist=True)
    output = safe_path(output)
    if not db.is_file() or not prices_dir.is_dir() or not flat_cache.is_dir():
        raise ValueError("freeze sources must already exist")
    for source in (db, prices_dir, flat_cache):
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("research destination overlaps a source")
    source_hash = hash_file(db)
    fingerprints = {db: source_hash}
    calendar = SessionCalendar()
    from stocksweeper.forecast.physical_contest import (
        EGARCH_VERSION,
        EMPIRICAL_SHADOW_VERSION,
        GJR_VERSION,
        HAR_VERSION,
        MARKOV_VERSION,
        NGBOOST_VERSION,
        SKEW_T_VERSION,
        STUDENT_VERSION,
    )
    from stocksweeper.forecast.predictive import BASELINE_VERSION
    from stocksweeper.research.historical_options.runner import SETTINGS
    with atomic_directory(output) as stage:
        with duckdb.connect(str(db), read_only=True, config=_DB_CONFIG) as connection:
            connection.execute("SET TimeZone='UTC'")
            connection.execute("BEGIN TRANSACTION")
            schemas = _schemas(connection)
            counts = {
                name: connection.execute(f"SELECT COUNT(*) FROM massive.{name}").fetchone()[0]
                for name in _TABLES
            }
            contracts, reference_counts = _contracts(connection, schemas, calendar)
            tickers = sorted(contracts.filter(pl.col("valid"))["ticker"].unique().to_list())
            stocks, stock_metadata, stock_fingerprints = _capture_prices(prices_dir, tickers)
            fingerprints.update(stock_fingerprints)
            stocks.write_parquet(stage / "stocks.parquet", compression="zstd")
            contracts.write_parquet(stage / "contracts.parquet", compression="zstd")
            connection.register("normalized_contracts", contracts.to_arrow())
            window = connection.execute(
                "SELECT MIN(CAST(timezone('America/New_York',bar_start) AS DATE)),"
                "MAX(CAST(timezone('America/New_York',bar_start) AS DATE)) "
                "FROM massive.option_hour_bars"
            ).fetchone()
            if window[0] is None or window[1] is None:
                raise ValueError("empty hourly archive")
            connection.register("calendar_sessions", session_buckets(calendar, *window).to_arrow())
            hour_count, hour_duplicates = _hours(connection, schemas, stage / "hours.parquet")
            rest_count = _rest_validation(connection, schemas, stage / "rest_validation.parquet")
            fetch_count = _fetches(connection, schemas, stage / "fetches.parquet")
            connection.execute("COMMIT")
        flat_count, flat_fingerprints = _flat_validation(
            flat_cache, contracts, stage / "flat_validation.parquet"
        )
        fingerprints.update(flat_fingerprints)
        # Stream concatenation rather than materializing the million-row tables.
        pl.concat([
            pl.scan_parquet(stage / "rest_validation.parquet"),
            pl.scan_parquet(stage / "flat_validation.parquet"),
        ], how="vertical_relaxed").sort(["contract", "session", "source", "source_t"]).sink_parquet(
            stage / "daily_validation.parquet", compression="zstd"
        )
        (stage / "rest_validation.parquet").unlink()
        (stage / "flat_validation.parquet").unlink()
        coverage = _derive_days(stage)
        daily_sources = {
            row["source"]: row["rows"]
            for row in pl.scan_parquet(stage / "daily_validation.parquet").group_by("source")
            .agg(pl.len().alias("rows")).collect(engine="streaming").iter_rows(named=True)
        }
        exclusions = _exclusions(stage, reference_counts["identical_duplicates"])
        if counts["option_hour_bars"] != hour_count or counts["option_bars"] != rest_count:
            raise ValueError("archive row accounting failed")
        current_flat_files = set(flat_cache.glob("*.json.gz"))
        if current_flat_files != set(flat_fingerprints) or any(
            not path.is_file() or hash_file(path) != expected
            for path, expected in fingerprints.items()
        ):
            raise ValueError("source changed during capture; retry freeze")
        fetches = pl.read_parquet(stage / "fetches.parquet").join(
            contracts.select("contract", "ticker", "expiry_session"),
            on="contract", how="left",
        ).with_columns(
            pl.when(pl.col("expiry_session") > pl.lit(window[1], dtype=pl.Date))
            .then(pl.lit(window[1], dtype=pl.Date)).otherwise(pl.col("expiry_session"))
            .alias("expected_cutoff")
        )
        fetch_audit = fetches.select(
            pl.len().alias("rows"),
            ((pl.col("kind") == "hour") & pl.col("daily_days").is_null()).sum().alias(
                "legacy_hour_fetch_rows"
            ),
            pl.col("volume_mismatch_days").fill_null(0).sum().alias("reported_volume_mismatch_days"),
            pl.col("to_date").min().alias("oldest_fetch_cutoff"),
            pl.col("to_date").max().alias("latest_fetch_cutoff"),
            ((pl.col("kind") == "hour") & (pl.col("to_date") < window[1])).sum().alias(
                "fetch_windows_ending_before_archive_cutoff"
            ),
            ((pl.col("kind") == "hour") & (pl.col("to_date") < pl.col("expected_cutoff")))
            .sum().alias("stale_hour_fetch_windows"),
            ((pl.col("kind") == "hour") & pl.col("from_date").is_null()).sum().alias(
                "unknown_hour_fetch_start"
            ),
            ((pl.col("kind") == "hour") & pl.col("to_date").is_null()).sum().alias(
                "unknown_hour_fetch_end"
            ),
        ).row(0, named=True)
        fetch_audit["contracts_without_hour_fetch_ledger"] = contracts.join(
            fetches.filter(pl.col("kind") == "hour").select("contract").unique(),
            on="contract", how="anti",
        ).height
        fetch_audit["historical_listing_completeness"] = "unknown"
        fetch_audit["pre_first_observation_coverage"] = "unknown"
        fetch_audit["missing_hour_is_known_nontrading"] = False
        fetch_audit["by_ticker_kind"] = fetches.group_by(["ticker", "kind"]).agg(
            pl.len().alias("rows"),
            (pl.col("expiry_session").is_not_null()
             & (pl.col("to_date") < pl.col("expected_cutoff")))
            .sum().alias("stale_vs_needed_cutoff"),
            (pl.col("from_date").is_null() | pl.col("to_date").is_null())
            .sum().alias("unknown_fetch_window"),
            ((pl.col("kind") == "hour") & pl.col("daily_days").is_null())
            .sum().alias("legacy_unknown_daily_days"),
        ).sort(["ticker", "kind"]).to_dicts()
        source_fingerprint = {
            "database_sha256": source_hash,
            "stock_cache_files": {
                f"{path.parent.name}/{path.name}": expected
                for path, expected in stock_fingerprints.items()
            },
            "filtered_flat_cache": {
                path.name: expected for path, expected in flat_fingerprints.items()
            },
        }
        metadata = {
            "artifact_type": "historical_options_snapshot", "version": SNAPSHOT_VERSION,
            "provenance": "current_vintage_retrospective",
            "source_fingerprints": source_fingerprint, "source_schemas": schemas,
            "source_counts": counts, "normalization_counts": {
                "reference_raw": reference_counts["raw"], "reference_normalized": contracts.height,
                "reference_identical_duplicates": len(reference_counts["identical_duplicates"]),
                "hourly_rows": hour_count, "hourly_duplicate_keys": hour_duplicates,
                "database_daily_rows": rest_count, "filtered_flat_daily_rows": flat_count,
                "fetch_rows": fetch_count, **coverage,
            },
            "daily_validation_sources": daily_sources,
            "exclusion_counts": exclusions, "stock_metadata": stock_metadata,
            "archive_first_session": window[0].isoformat(),
            "archive_last_session": window[1].isoformat(), "fetch_audit": fetch_audit,
            "settings": {
                "calendar": "XNYS", "timezone": "America/New_York", "shares_per_contract": 100,
                "hourly_primary": True, "missing_is_zero": False,
                "primary_excludes_reconciliation_failures": True,
                "ordinary_terms_only": True, "download": False,
                "flat_adjusted": False, "unknown_listing_coverage": True,
                "bucket_timestamp_is_trade_timestamp": False,
            },
            "environment": environment_metadata(),
            "study_settings": SETTINGS,
            "model_versions": {
                "lognormal_ewma": BASELINE_VERSION, "empirical_scaled": EMPIRICAL_SHADOW_VERSION,
                "student_t_ewma": STUDENT_VERSION, "gjr_garch_t": GJR_VERSION,
                "ohlc_har": HAR_VERSION, "skew_t_ewma": SKEW_T_VERSION,
                "egarch_skew_t": EGARCH_VERSION, "markov_switching": MARKOV_VERSION,
                "ngboost_pooled": NGBOOST_VERSION,
            },
            "runtime": {
                "created_at": datetime.now(UTC).isoformat(),
                "duration_seconds": time.perf_counter() - started,
                "peak_rss_bytes": int(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    * (1 if sys.platform == "darwin" else 1024)
                ),
                "memory_target_bytes": 4 * 1024**3,
            },
        }
        result = finalize_manifest(stage, metadata)
    return result


def verify_snapshot(path: Path) -> dict:
    manifest = verify_artifact(path)
    required = {
        "contracts.parquet", "hours.parquet", "days.parquet", "stocks.parquet",
        "exclusions.parquet", "fetches.parquet", "daily_validation.parquet",
    }
    if (
        manifest.get("artifact_type") != "historical_options_snapshot"
        or manifest.get("version") != SNAPSHOT_VERSION
        or set(manifest["files"]) != required
    ):
        raise ValueError("unsupported or incomplete historical option snapshot")
    return manifest
