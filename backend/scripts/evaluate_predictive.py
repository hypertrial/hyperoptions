"""Reproducible, local rolling-origin forecast evaluation.

Usage: uv run python scripts/evaluate_predictive.py AAPL [--as-of 2026-09-25]
Add --refresh to fetch Yahoo first; the default reads only the verified cache.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter

from stocksweeper.config import load_settings
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import ForecastPriceStore, YahooForecastProvider
from stocksweeper.forecast.predictive import PredictiveForecaster, evaluate_predictive_history


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="Nasdaq ticker with prepared Yahoo Close history")
    parser.add_argument("--as-of", type=date.fromisoformat, help="completed session to evaluate")
    parser.add_argument("--data-dir", type=Path, help="override STOCKSWEEPER_DATA_DIR")
    parser.add_argument("--refresh", action="store_true", help="refresh Yahoo history first")
    args = parser.parse_args()
    ticker = args.ticker.upper()
    calendar = SessionCalendar()
    data_dir = args.data_dir or load_settings().resolved_data_dir()
    store = ForecastPriceStore(data_dir, YahooForecastProvider())
    # Validates ticker syntax before any fetch or filesystem read.
    store.path(ticker)
    latest = calendar.last_completed(datetime.now(UTC))
    completed = args.as_of or latest
    if completed > latest:
        parser.error("--as-of must be a completed session")
    latency_ms = None
    if args.refresh:
        if completed != latest:
            parser.error("--refresh is only supported for the latest completed session")
        start = perf_counter()
        PredictiveForecaster(data_dir, calendar=calendar).prepare(ticker, completed)
        latency_ms = round((perf_counter() - start) * 1000, 3)
    try:
        frame = store.read(ticker)
        if frame is None:
            parser.error(f"no verified local Yahoo Close history for {ticker}; use --refresh")
        report = evaluate_predictive_history(frame, completed, calendar)
    except ValueError as exc:
        parser.error(str(exc))
    report["ticker"] = ticker
    report["refresh_latency_ms"] = latency_ms
    print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
