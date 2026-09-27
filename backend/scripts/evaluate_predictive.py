"""Reproducible, local rolling-origin forecast evaluation.

Usage: uv run python scripts/evaluate_predictive.py AAPL [--as-of 2026-09-25]
Add --refresh to fetch Yahoo first; the default reads only the verified cache.
Paired as-issued scoring: uv run python scripts/evaluate_predictive.py
    --ledger-contest --candidate student_t_ewma --period holdout
    --holdout-start 2026-10-01
Immutable-replay evidence is screening only and can never promote a live model.
Frozen-cohort screen: uv run python scripts/evaluate_predictive.py
    --replay-cohort --candidate student_t_ewma --period screen
    [--max-origins 20] [--max-tickers 50]
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from math import exp, sqrt
from pathlib import Path
from time import perf_counter

import polars as pl

from options_api.market_calendar import session_close
from stocksweeper.config import load_settings
from stocksweeper.forecast.audit import read_audit_cohort
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.forecast.market import ForecastPriceStore, YahooForecastProvider
from stocksweeper.forecast.physical_contest import PhysicalShadowForecaster
from stocksweeper.forecast.physical_evaluation import ContestRow, crps, evaluate_band
from stocksweeper.forecast.predictive import PredictiveForecaster, evaluate_predictive_history

_STRIKE_GRID = (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5)
_BANDS = {"1": range(1, 2), "2-5": range(2, 6), "6-25": range(6, 26)}


class _FrozenPrices:
    def __init__(self, frame):
        self.frame = frame

    def read(self, ticker):
        return self.frame


def _replay_cohort(
    data_dir: Path,
    calendar: SessionCalendar,
    candidate: str,
    *,
    max_origins: int,
    max_tickers: int,
    period: str,
    holdout_start: date | None,
) -> dict[str, object]:
    cohort = read_audit_cohort(data_dir)
    if cohort is None:
        raise ValueError("freeze the verified 50-ticker audit cohort first")
    if not 1 <= max_tickers <= len(cohort.members) or not 1 <= max_origins <= 40:
        raise ValueError("replay ticker/origin limits are out of range")
    cutoff = holdout_start or cohort.completed_session - timedelta(days=180)
    if period == "all":
        period = "screen"
    if period not in ("screen", "holdout"):
        raise ValueError("replay period must be screen or holdout")
    origin_sessions = calendar.sessions(
        cohort.completed_session - timedelta(days=1096), cohort.completed_session
    )
    # One common, embargoed calendar block schedule for all tickers and horizons.
    origins = origin_sessions[:-25:26][-max_origins:]
    if not origins:
        raise ValueError("audit snapshot has no mature rolling origins")
    reports = {}
    skipped: dict[str, int] = {}
    frames = {}
    shadows = {}
    for member in cohort.members[:max_tickers]:
        frame = pl.read_parquet(member.snapshot)
        frames[member.ticker] = frame
        forecaster = PredictiveForecaster(data_dir, calendar=calendar)
        forecaster.prices = _FrozenPrices(frame)
        shadows[member.ticker] = PhysicalShadowForecaster(forecaster)
    for band, horizons in _BANDS.items():
        rows: list[ContestRow] = []
        for ticker, frame in frames.items():
            closes = dict(frame.select("ts", "close").iter_rows())
            split_dates = {
                day for day, amount in frame.select("ts", "stock_splits").iter_rows() if amount > 0
            }
            shadow = shadows[ticker]
            for origin in origins:
                when = datetime.combine(origin, time(23), tzinfo=UTC)
                for horizon in horizons:
                    expiry = calendar.offset(origin, horizon)
                    results = shadow.forecast_candidates(ticker, when, expiry)
                    baseline = results["lognormal_ewma"].distribution
                    if baseline is None:
                        reason = results["lognormal_ewma"].reason or "baseline_unavailable"
                        skipped[reason] = skipped.get(reason, 0) + 1
                        continue
                    assert baseline.spot is not None and baseline.daily_volatility is not None
                    outcome = closes.get(expiry)
                    if outcome is None:
                        skipped["maturity_close_missing"] = (
                            skipped.get("maturity_close_missing", 0) + 1
                        )
                    elif any(origin < day <= expiry for day in split_dates):
                        outcome = None
                        skipped["split_affected_label"] = skipped.get("split_affected_label", 0) + 1
                    regime = (
                        "low"
                        if baseline.daily_volatility < 0.02
                        else "medium"
                        if baseline.daily_volatility < 0.05
                        else "high"
                    )
                    for method in ("lognormal_ewma", candidate):
                        shadow_result = results[method]
                        distribution = shadow_result.distribution
                        full_score = (
                            crps(distribution.prices, distribution.weights, outcome)
                            if distribution is not None and outcome is not None
                            else None
                        )
                        for threshold in _STRIKE_GRID:
                            strike = baseline.spot * exp(
                                baseline.daily_volatility * sqrt(horizon) * threshold
                            )
                            bucket = (
                                "near_atm"
                                if abs(threshold) <= 0.5
                                else "moderate"
                                if abs(threshold) <= 1.0
                                else "tail"
                            )
                            for side in ("call", "put"):
                                observed = (
                                    (outcome > strike if side == "call" else outcome < strike)
                                    if outcome is not None
                                    else None
                                )
                                rows.append(
                                    ContestRow(
                                        ticker=ticker,
                                        origin=origin,
                                        expiry_session=expiry,
                                        horizon=horizon,
                                        strike=format(strike, ".12g"),
                                        side=side,
                                        method=method,
                                        probability=distribution.probability(side, strike)
                                        if distribution is not None
                                        else None,
                                        observed_itm=observed,
                                        provenance="immutable_replay",
                                        moneyness=bucket,
                                        volatility_regime=regime,
                                        event_status="unknown",
                                        reason=shadow_result.reason,
                                        input_vintage=baseline.data_hash,
                                        issued_at=when,
                                        issuance_key=f"{method}|{horizon}|{threshold}|{side}",
                                        contract_id=f"{ticker}|{expiry}|{threshold}|{side}",
                                        crps=full_score,
                                        prepare_ms=shadow_result.prepare_ms,
                                        lookup_ms=shadow_result.lookup_ms,
                                    )
                                )
        reports[band] = evaluate_band(
            rows, candidate, band, holdout_start=cutoff, period=period, calendar=calendar
        )
    return {
        "source": "immutable current-vintage Yahoo audit snapshots",
        "provenance": "immutable_replay",
        "warning": (
            "Retrospective Yahoo current-vintage, synthetic-strike labels. "
            "Screening only; never promotion evidence."
        ),
        "audit_session": cohort.completed_session.isoformat(),
        "audit_frozen_at": cohort.frozen_at.isoformat(),
        "cohort_size": len(cohort.members),
        "tickers_evaluated": max_tickers,
        "calendar_origin_blocks": len(origins),
        "holdout_start": cutoff.isoformat(),
        "period": period,
        "skipped": skipped,
        "bands": reports,
    }


def _ledger_band_rows(
    ledger: ForecastLedger,
    calendar: SessionCalendar,
    provenance: str,
    candidate: str,
    holdout_start: date | None,
    period: str,
    band: str,
) -> list[ContestRow]:
    contest_rows: list[ContestRow] = []
    horizons = _BANDS[band]
    issues = ledger.iter_evaluation_rows(
        provenance=provenance,
        since=holdout_start if period == "holdout" else None,
        expiry_before=holdout_start if period == "screen" else None,
        methods=("lognormal_ewma", candidate),
        horizon_range=(horizons.start, horizons.stop - 1),
        with_crps=True,
    )
    for item in issues:
        origin = item["input_session"]
        method = item["method"]
        if origin is None or method is None:
            continue
        label_valid = (
            item["label_status"] == "valid"
            and item["label_checked_at"] > item["issued_at"]
            and item["issued_at"] < session_close(item["expiry_session"])
        )
        probability = item["itm_probability"] if item["status"] == "available" else None
        observed = bool(item["observed_itm"]) if label_valid else None
        score = item["crps"] if label_valid else None
        strike = Decimal(item["strike_exact"])
        spot = Decimal(item["spot_exact"]) if item["spot_exact"] else None
        relative = abs(float(strike / spot - 1)) if spot else None
        moneyness = (
            "unknown"
            if relative is None
            else "near_atm"
            if relative <= 0.05
            else "moderate"
            if relative <= 0.15
            else "tail"
        )
        contest_rows.append(
            ContestRow(
                ticker=item["ticker"],
                origin=origin,
                expiry_session=item["expiry_session"],
                horizon=calendar.horizon(origin, item["expiration"]),
                strike=item["strike_exact"],
                side=item["side"],
                method=method,
                probability=probability,
                observed_itm=observed,
                provenance=provenance,
                moneyness=moneyness,
                volatility_regime=item["volatility_regime"] or "unknown",
                event_status=item["known_event_status"] or "unknown",
                reason=item["unavailable_reason"] or item["label_reason"],
                input_vintage=item["data_hash"],
                issued_at=item["issued_at"],
                issuance_key=item["idempotency_key"],
                contract_id=item["contract_key"],
                crps=score,
                prepare_ms=item["prepare_ms"],
                lookup_ms=item["lookup_ms"],
            )
        )
    return contest_rows


def _ledger_contest(
    ledger: ForecastLedger,
    calendar: SessionCalendar,
    provenance: str,
    candidate: str,
    holdout_start: date | None,
    period: str,
) -> dict[str, object]:
    return {
        "source": "append-only forecast ledger",
        "provenance": provenance,
        "skipped_attempts": ledger.evaluation_skipped_attempts(provenance),
        "prospective_panel_coverage": (
            ledger.panel_coverage(
                since=holdout_start if period == "holdout" else None,
                before=holdout_start if period == "screen" else None,
            )
            if provenance == "as_issued"
            else None
        ),
        "bands": {
            band: evaluate_band(
                _ledger_band_rows(
                    ledger, calendar, provenance, candidate, holdout_start, period, band
                ),
                candidate,
                band,
                holdout_start=holdout_start,
                period=period,
                calendar=calendar,
            )
            for band in ("1", "2-5", "6-25")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", nargs="?", help="Nasdaq ticker with prepared Yahoo Close history")
    parser.add_argument("--as-of", type=date.fromisoformat, help="completed session to evaluate")
    parser.add_argument("--data-dir", type=Path, help="override STOCKSWEEPER_DATA_DIR")
    parser.add_argument("--refresh", action="store_true", help="refresh Yahoo history first")
    parser.add_argument(
        "--ledger-contest",
        action="store_true",
        help="score paired as-issued or immutable-replay model issuances from DuckDB",
    )
    parser.add_argument(
        "--replay-cohort",
        action="store_true",
        help="screen challengers against frozen current-vintage audit snapshots",
    )
    parser.add_argument("--max-origins", type=int, default=20)
    parser.add_argument("--max-tickers", type=int, default=50)
    parser.add_argument(
        "--candidate", choices=("empirical_scaled", "student_t_ewma", "gjr_garch_t")
    )
    parser.add_argument(
        "--provenance", choices=("as_issued", "immutable_replay"), default="as_issued"
    )
    parser.add_argument("--holdout-start", type=date.fromisoformat)
    parser.add_argument("--period", choices=("all", "screen", "holdout"), default="all")
    args = parser.parse_args()
    calendar = SessionCalendar()
    data_dir = args.data_dir or load_settings().resolved_data_dir()
    if args.replay_cohort:
        if args.ticker or args.refresh or args.ledger_contest or not args.candidate:
            parser.error("--replay-cohort needs --candidate and no ticker, refresh, or ledger")
        try:
            report = _replay_cohort(
                data_dir,
                calendar,
                args.candidate,
                max_origins=args.max_origins,
                max_tickers=args.max_tickers,
                period=args.period,
                holdout_start=args.holdout_start,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
        return
    if args.ledger_contest:
        if args.ticker or args.refresh or not args.candidate:
            parser.error("--ledger-contest needs --candidate and no ticker or --refresh")
        if args.period != "all" and args.holdout_start is None:
            parser.error("--period screen/holdout needs --holdout-start")
        report = _ledger_contest(
            ForecastLedger(data_dir),
            calendar,
            args.provenance,
            args.candidate,
            args.holdout_start,
            args.period,
        )
        print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
        return
    if args.ticker is None:
        parser.error("ticker is required unless a contest is selected")
    ticker = args.ticker.upper()
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
