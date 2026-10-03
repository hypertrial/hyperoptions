"""Screen intraday conditioning at historical Opens using current-vintage cache.

This is retrospective research, never prospective as-issued accuracy evidence.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from decimal import Decimal
from math import isfinite
from pathlib import Path

import polars as pl

from options_api.intraday_shadow import forecast_intraday_shadow
from options_api.market_calendar import _calendar
from options_api.market_watch import UnderlyingQuote
from stocksweeper.config import load_settings
from stocksweeper.forecast.predictive import PredictiveForecaster

HORIZONS = (1, 2, 5, 25)
STRIKE_RATIOS = (0.9, 0.95, 1.0, 1.05, 1.1)


def evaluate(ticker: str, data_dir: Path, max_origins: int) -> dict[str, object]:
    forecaster = PredictiveForecaster(data_dir)
    frame = forecaster.prices.read(ticker)
    if frame is None or frame.is_empty():
        raise ValueError("verified price cache is unavailable")
    sessions = forecaster.calendar.sessions(frame["ts"][0], frame["ts"][-1])
    bars = {row["ts"]: row for row in frame.iter_rows(named=True)}
    scores: dict[int, dict[str, list[float]]] = defaultdict(
        lambda: {"dated_close": [], "simple_reanchor": [], "intraday_shadow": [], "lookup_ms": []}
    )
    rejected: dict[str, int] = defaultdict(int)
    candidates = range(max(61, len(sessions) - max_origins), len(sessions))
    for index in candidates:
        day = sessions[index]
        origin = sessions[index - 1]
        bar = bars.get(day)
        if bar is None:
            rejected["origin_open_missing"] += 1
            continue
        session = _calendar().date_to_session(day.isoformat(), direction="none")
        opened = _calendar().session_open(session).to_pydatetime()
        quote = UnderlyingQuote(
            Decimal(str(bar["open"])),
            day,
            "historical_open",
            opened,
            opened,
        )
        history = frame.filter(pl.col("ts") <= origin)
        for horizon in HORIZONS:
            maturity_index = index + horizon - 1
            if maturity_index >= len(sessions):
                continue
            maturity = sessions[maturity_index]
            outcome = bars.get(maturity)
            if outcome is None:
                rejected["maturity_close_missing"] += 1
                continue
            close = outcome["close"]
            if close is None or not isfinite(close) or close <= 0:
                rejected["maturity_close_invalid"] += 1
                continue
            if any(
                bars.get(session, {}).get("stock_splits", 0) > 0
                for session in sessions[index : maturity_index + 1]
            ):
                rejected["split_affected_label"] += 1
                continue
            started = time.monotonic()
            baseline = forecaster.forecast(ticker, opened, maturity)
            if baseline.status != "available" or baseline.spot is None:
                rejected[baseline.reason or "baseline_unavailable"] += 1
                continue
            shadow = forecast_intraday_shadow(
                forecaster,
                baseline,
                quote,
                historical_input=history,
            )
            if shadow.distribution is None:
                rejected[shadow.reason or "shadow_unavailable"] += 1
                continue
            elapsed_ms = (time.monotonic() - started) * 1000
            unit = {"dated_close": [], "simple_reanchor": [], "intraday_shadow": []}
            for ratio in STRIKE_RATIOS:
                strike = Decimal(str(round(baseline.spot * ratio, 3)))
                for side in ("call", "put"):
                    label = (
                        (close > float(strike))
                        if side == "call"
                        else (close < float(strike))
                    )
                    dated = baseline.probability(side, strike)
                    intraday = shadow.distribution.probability(side, strike)
                    if dated is None or intraday is None:
                        continue
                    reanchored = [
                        price * float(quote.spot) / baseline.spot for price in baseline.prices
                    ]
                    simple = sum(
                        weight
                        for price, weight in zip(reanchored, baseline.weights, strict=True)
                        if (price > float(strike) if side == "call" else price < float(strike))
                    )
                    for name, probability in (
                        ("dated_close", dated),
                        ("simple_reanchor", simple),
                        ("intraday_shadow", intraday),
                    ):
                        unit[name].append((probability - label) ** 2)
            if all(unit.values()):
                for name, values in unit.items():
                    scores[horizon][name].append(sum(values) / len(values))
                scores[horizon]["lookup_ms"].append(elapsed_ms)
    return {
        "ticker": ticker,
        "provenance": "retrospective_current_vintage_historical_open_screen_only",
        "through_session": frame["ts"][-1].isoformat(),
        "max_origins": max_origins,
        "horizons": {
            str(horizon): {
                "scored_ticker_origin_horizon_units": len(bucket["dated_close"]),
                "mean_brier": {
                    name: sum(bucket[name]) / len(bucket[name]) if bucket[name] else None
                    for name in ("dated_close", "simple_reanchor", "intraday_shadow")
                },
                "p50_lookup_ms": sorted(bucket["lookup_ms"])[len(bucket["lookup_ms"]) // 2]
                if bucket["lookup_ms"]
                else None,
            }
            for horizon, bucket in scores.items()
        },
        "rejection_reasons": dict(sorted(rejected.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--data-dir", type=Path, default=load_settings().resolved_data_dir())
    parser.add_argument("--max-origins", type=int, default=80)
    args = parser.parse_args()
    if not 1 <= args.max_origins <= 500:
        parser.error("--max-origins must be between 1 and 500")
    print(json.dumps(evaluate(args.ticker.upper(), args.data_dir, args.max_origins), indent=2))


if __name__ == "__main__":
    main()
