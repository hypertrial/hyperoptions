"""Compute and cache indicator columns.

Polars owns storage. Pandas exists only at the pandas-ta boundary.
A later run computes columns that are missing and leaves the rest untouched
when the bar index and prices have not changed. A price correction with the
same dates recomputes every cached column, not only the ones just requested.
"""

from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

import pandas as pd
import polars as pl

from stocksweeper.indicators.names import INDICATORS, IndicatorRequest, declared_columns
from stocksweeper.indicators.registry import SPECS, output_columns

_CACHE_LOCK = threading.Lock()
_PRICE_COLUMNS = frozenset({"ts", "open", "high", "low", "close", "volume", "ticker"})


def compute_indicators(ohlcv: pl.DataFrame, requests: list[IndicatorRequest]) -> pl.DataFrame:
    if not requests:
        return ohlcv
    pdf = ohlcv.to_pandas()
    columns: dict[str, pd.Series] = {}
    for request in _unique(requests):
        produced = SPECS[request.name](pdf, request.as_dict())
        expected = output_columns(request)
        missing = [name for name in expected if name not in produced]
        if missing:
            raise RuntimeError(f"{request.name} did not produce {missing}")
        for name in expected:
            columns[name] = produced[name]
    extra = pl.from_pandas(pd.DataFrame(columns))
    return pl.concat([ohlcv, extra], how="horizontal_extend")


def ensure_indicators(
    ohlcv: pl.DataFrame,
    requests: list[IndicatorRequest],
    cache_path: Path,
) -> pl.DataFrame:
    with _CACHE_LOCK:
        unique = _unique(requests)
        cached = pl.read_parquet(cache_path) if cache_path.exists() else None
        same_bars = cached is not None and _same_bars(cached, ohlcv)
        if cached is not None and same_bars:
            missing = [
                request
                for request in unique
                if any(column not in cached.columns for column in output_columns(request))
            ]
            if not missing:
                return cached
            extra_columns: list[str] = []
            for request in missing:
                for column in output_columns(request):
                    if column not in cached.columns and column not in extra_columns:
                        extra_columns.append(column)
            extra = compute_indicators(ohlcv, missing).select(extra_columns)
            merged = pl.concat([cached, extra], how="horizontal_extend")
            _write(cache_path, merged)
            return merged
        previous = _requests_from_columns(cached.columns) if cached is not None else []
        fresh = compute_indicators(ohlcv, _unique([*previous, *unique]))
        _write(cache_path, fresh)
        return fresh


def _same_bars(cached: pl.DataFrame, ohlcv: pl.DataFrame) -> bool:
    columns = ["ts", "open", "high", "low", "close", "volume"]
    if any(column not in cached.columns for column in columns):
        return False
    return cached.select(columns).equals(ohlcv.select(columns))


def _unique(requests: list[IndicatorRequest]) -> list[IndicatorRequest]:
    seen: dict[IndicatorRequest, None] = {}
    for request in requests:
        seen.setdefault(request, None)
    return list(seen)


def _requests_from_columns(columns: list[str]) -> list[IndicatorRequest]:
    """Rebuild indicator requests from cache column names."""
    names = sorted(INDICATORS, key=len, reverse=True)
    found: dict[IndicatorRequest, None] = {}
    for column in columns:
        if column in _PRICE_COLUMNS:
            continue
        for name in names:
            prefix = column == name or column.startswith(name + "_")
            if not prefix:
                continue
            request = _request_from_column(name, column)
            if request is not None:
                found.setdefault(request, None)
            break
    return list(found)


def _request_from_column(name: str, column: str) -> IndicatorRequest | None:
    decl = INDICATORS[name]
    rest = column[len(name) :].removeprefix("_")
    for suffix in sorted((item for item in decl.suffixes if item), key=len, reverse=True):
        if rest == suffix:
            rest = ""
            break
        marker = "_" + suffix
        if rest.endswith(marker):
            rest = rest[: -len(marker)]
            break
    parts = rest.split("_") if rest else []
    if len(parts) != len(decl.params):
        return None
    params = {key: _parse_param(part) for key, part in zip(decl.params, parts, strict=True)}
    expected = set(declared_columns(name, params))
    if column not in expected and column not in decl.extra:
        return None
    return IndicatorRequest.make(name, params)


def _parse_param(text: str) -> int | float:
    if "." in text:
        return float(text)
    return int(text)


def _write(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    frame.write_parquet(temporary)
    temporary.replace(path)
