"""Dividend-unadjusted, split-normalized daily prices kept apart from research bars."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import pandas as pd
import polars as pl

from stocksweeper.forecast.calendar import SessionCalendar

PRICE_COLUMNS = ("ts", "open", "high", "low", "close", "volume", "dividends", "stock_splits")
EARLIEST_SESSION = date(1970, 1, 1)
MAX_PRICE_BARS = 20_000
TAIL_OVERLAP_BARS = 30


class ForecastProvider(Protocol):
    def fetch(self, ticker: str, start: date | None, end: date) -> pl.DataFrame: ...


class YahooForecastProvider:
    def fetch(self, ticker: str, start: date | None, end: date) -> pl.DataFrame:
        import yfinance as yf

        arguments: dict[str, object] = {
            "auto_adjust": False,
            "actions": True,
            "interval": "1d",
            "end": (end + timedelta(days=1)).isoformat(),
            "timeout": 20,
            "raise_errors": True,
        }
        arguments["start"] = (start or EARLIEST_SESSION).isoformat()
        history = yf.Ticker(ticker).history(**arguments)
        return normalize_prices(history, ticker)


def normalize_prices(frame: pd.DataFrame | pl.DataFrame, ticker: str) -> pl.DataFrame:
    if isinstance(frame, pd.DataFrame):
        if len(frame) > MAX_PRICE_BARS:
            raise ValueError(f"{ticker} forecast history exceeds the bounded bar limit")
        frame = pl.from_pandas(frame.reset_index())
    if frame.is_empty():
        return pl.DataFrame(
            schema={name: pl.Date if name == "ts" else pl.Float64 for name in PRICE_COLUMNS}
        )
    if frame.height > MAX_PRICE_BARS:
        raise ValueError(f"{ticker} forecast history exceeds the bounded bar limit")
    names = {name: name.strip().lower().replace(" ", "_") for name in frame.columns}
    frame = frame.rename(names)
    for candidate in ("date", "datetime", "index"):
        if "ts" not in frame.columns and candidate in frame.columns:
            frame = frame.rename({candidate: "ts"})
            break
    missing = [
        name
        for name in ("ts", "open", "high", "low", "close", "volume")
        if name not in frame.columns
    ]
    if missing:
        raise ValueError(f"{ticker} forecast prices missing {', '.join(missing)}")
    if "stock_splits" not in frame.columns:
        raise ValueError(f"{ticker} forecast prices missing split action metadata")
    if "dividends" not in frame.columns:
        raise ValueError(f"{ticker} forecast prices missing dividend action metadata")
    frame = frame.select(PRICE_COLUMNS).with_columns(
        pl.col("ts").cast(pl.Date),
        *[pl.col(name).cast(pl.Float64) for name in PRICE_COLUMNS if name != "ts"],
    )
    if frame["ts"].n_unique() != frame.height:
        raise ValueError(f"{ticker} forecast prices have duplicate sessions")
    return frame.sort("ts")


def clean_completed(
    frame: pl.DataFrame, completed: date, calendar: SessionCalendar
) -> pl.DataFrame:
    if frame.is_empty():
        return frame
    sessions = calendar.sessions(frame["ts"][0], completed)
    valid = pl.col("ts").is_in(sessions) & (pl.col("ts") <= completed)
    for name in ("open", "high", "low", "close"):
        valid = valid & pl.col(name).is_finite() & (pl.col(name) > 0)
    valid = valid & (pl.col("high") >= pl.col("low"))
    valid = valid & (pl.col("open") >= pl.col("low")) & (pl.col("open") <= pl.col("high"))
    valid = valid & (pl.col("close") >= pl.col("low")) & (pl.col("close") <= pl.col("high"))
    valid = valid & pl.col("volume").is_finite() & (pl.col("volume") >= 0)
    valid = valid & pl.col("dividends").is_finite()
    valid = valid & pl.col("stock_splits").is_finite() & (pl.col("stock_splits") >= 0)
    return frame.filter(valid).sort("ts")


def price_hash(frame: pl.DataFrame) -> str:
    buffer = io.BytesIO()
    frame.select(PRICE_COLUMNS).write_ipc(buffer)
    return hashlib.sha256(buffer.getvalue()).hexdigest()


class ForecastPriceStore:
    def __init__(self, data_dir: Path, provider: ForecastProvider) -> None:
        self.root = data_dir / "forecast" / "prices"
        self.provider = provider

    def path(self, ticker: str) -> Path:
        if not ticker.isascii() or not ticker.replace(".", "").replace("-", "").isalnum():
            raise ValueError("invalid forecast ticker")
        return self.root / f"{ticker}.parquet"

    def read(self, ticker: str) -> pl.DataFrame | None:
        path = self.path(ticker)
        return pl.read_parquet(path) if path.is_file() else None

    def update(self, ticker: str, completed: date, *, full_refresh: bool = False) -> pl.DataFrame:
        path = self.path(ticker)
        existing = self.read(ticker)
        manifest = path.with_suffix(".json")
        previous_metadata = {}
        if manifest.is_file():
            try:
                previous_metadata = json.loads(manifest.read_text())
            except (OSError, ValueError):
                previous_metadata = {}
        month = completed.strftime("%Y-%m")
        use_full = (
            full_refresh
            or existing is None
            or previous_metadata.get("full_refreshed_month") != month
        )
        start = None
        if not use_full and existing is not None and not existing.is_empty():
            start = existing["ts"][max(0, existing.height - TAIL_OVERLAP_BARS)]
        fresh = normalize_prices(self.provider.fetch(ticker, start, completed), ticker)
        if fresh.is_empty() and (existing is None or use_full):
            raise ValueError(f"no forecast prices for {ticker}")

        if (
            existing is not None
            and not existing.is_empty()
            and not use_full
            and not fresh.is_empty()
        ):
            old_days = existing["ts"].to_list()
            new_days = fresh["ts"].to_list()
            old_overlap = existing.filter(pl.col("ts").is_in(new_days)).sort("ts")
            new_overlap = fresh.filter(pl.col("ts").is_in(old_days)).sort("ts")
            old_split_days = {
                row["ts"]: row["stock_splits"]
                for row in existing.iter_rows(named=True)
                if row["stock_splits"] > 0
            }
            new_split = any(
                row["stock_splits"] > 0 and old_split_days.get(row["ts"]) != row["stock_splits"]
                for row in fresh.iter_rows(named=True)
            )
            if old_overlap.is_empty() or not old_overlap.equals(new_overlap) or new_split:
                # Yahoo may rebase every historical Close after a split. A
                # changed overlap cannot be stitched to the old prefix.
                fresh = normalize_prices(self.provider.fetch(ticker, None, completed), ticker)
                use_full = True

        if use_full and existing is not None and not existing.is_empty():
            # A partial full-history response must not silently erase cached
            # corporate-action sessions or mix pre/post-split price scales.
            omitted = existing.filter(
                (pl.col("ts") <= completed) & ~pl.col("ts").is_in(fresh["ts"].to_list())
            )
            if not omitted.is_empty():
                raise ValueError(f"full forecast refresh omitted cached sessions for {ticker}")
            cached_splits = existing.filter(pl.col("stock_splits") > 0)
            refreshed_splits = fresh.filter(pl.col("stock_splits") > 0)
            if not cached_splits.is_empty() and not set(cached_splits["ts"]).issubset(
                set(refreshed_splits["ts"])
            ):
                raise ValueError(f"full forecast refresh lost a cached split for {ticker}")
        if fresh.is_empty():
            combined = existing
        elif existing is None or use_full:
            combined = fresh
        else:
            # Replace only sessions returned by the provider. A sparse tail
            # response cannot delete a prior split or dividend action.
            combined = pl.concat(
                [existing.filter(~pl.col("ts").is_in(fresh["ts"].to_list())), fresh],
                how="vertical",
            )
        frame = combined.filter(pl.col("ts") <= completed).sort("ts")
        if frame.is_empty():
            raise ValueError(f"no completed forecast prices for {ticker}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        frame.write_parquet(temporary)
        temporary.replace(path)
        metadata = {
            "ticker": ticker,
            "source": "Yahoo Finance daily Close",
            "auto_adjust": False,
            "price_basis": "split-normalized, dividend-unadjusted",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "through_session": frame["ts"][-1].isoformat(),
            "requested_through_session": completed.isoformat(),
            "full_refreshed_month": month
            if use_full
            else previous_metadata.get("full_refreshed_month"),
            "hash": price_hash(frame),
        }
        temporary_manifest = manifest.with_name(f"{manifest.name}.{uuid4().hex}.tmp")
        temporary_manifest.write_text(json.dumps(metadata, sort_keys=True))
        temporary_manifest.replace(manifest)
        return frame
