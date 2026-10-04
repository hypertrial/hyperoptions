"""Causal current-vintage replay against observed strikes and expiry closes."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from math import isfinite, log
from pathlib import Path

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.physical_contest import PhysicalShadowForecaster
from stocksweeper.forecast.predictive import PredictiveForecaster
from stocksweeper.research.historical_options.payoff import (
    WeightedPayoffIntegrator,
    pinball_loss,
)

MODELS = (
    "lognormal_ewma", "empirical_scaled", "student_t_ewma", "gjr_garch_t",
    "ohlc_har", "skew_t_ewma", "egarch_skew_t", "markov_switching",
)
IDENTITY_STARTS = {"CIFR": date(2021, 8, 30), "WULF": date(2021, 12, 14)}
PRIMARY_START = date(2024, 10, 29)
PRIMARY_END = date(2025, 12, 31)
SUPPLEMENT_END = date(2026, 9, 28)

_FIELDS = {
    "panel": pa.string(), "ticker": pa.string(), "origin": pa.date32(),
    "expiry": pa.date32(), "expiry_session": pa.date32(), "contract": pa.string(),
    "side": pa.string(), "horizon": pa.int32(), "strike": pa.float64(),
    "decision_close": pa.float64(), "premium_mark": pa.float64(),
    "model": pa.string(), "model_version": pa.string(), "status": pa.string(),
    "reason": pa.string(), "score_reason": pa.string(), "primary_eligible": pa.bool_(),
    "forecast_available": pa.bool_(),
    "reconciliation_failed": pa.bool_(), "prediction": pa.float64(),
    "itm": pa.bool_(), "atm": pa.bool_(), "brier": pa.float64(),
    "log_loss": pa.float64(), "crps": pa.float64(), "normalized_crps": pa.float64(),
    "expected_pnl": pa.float64(), "realized_pnl": pa.float64(),
    "loss_probability": pa.float64(), "realized_loss": pa.bool_(),
    "quantile05": pa.float64(), "pinball": pa.float64(), "breach_strict": pa.bool_(),
    "breach_inclusive": pa.bool_(), "atom": pa.float64(), "quantile_atom": pa.float64(),
    "forecast_anchor": pa.float64(), "distribution_support": pa.int32(),
}
FORECAST_SCHEMA = pa.schema([pa.field(name, kind) for name, kind in _FIELDS.items()])


def panel_for(ticker: str, origin: date, maturity: date) -> str:
    if (
        ticker in ("CIFR", "IREN", "NBIS", "WULF")
        and PRIMARY_START <= origin <= PRIMARY_END and maturity <= PRIMARY_END
    ):
        return "primary"
    if (
        ticker in ("CIFR", "IREN", "WULF")
        and date(2026, 1, 1) <= origin <= SUPPLEMENT_END
        and maturity <= SUPPLEMENT_END
    ):
        return "supplement"
    return "other_eligible"


class FrozenPrices:
    """A read-only adapter with no provider, prepare, update, or refresh method."""

    def __init__(self, frames: dict[str, pl.DataFrame]) -> None:
        self._frames = frames

    def read(self, ticker: str) -> pl.DataFrame | None:
        return self._frames.get(ticker)


def _frames(stocks: pl.DataFrame) -> dict[str, pl.DataFrame]:
    frames = {}
    for key, frame in stocks.partition_by("ticker", as_dict=True).items():
        ticker = key[0]
        frame = frame.drop("ticker").sort("ts")
        start = IDENTITY_STARTS.get(ticker)
        if start is not None:
            frame = frame.filter(pl.col("ts") >= start)
        frames[ticker] = frame
    return frames


def _action_reason(
    frame: pl.DataFrame | None, origin: date, expiry: date, calendar: SessionCalendar
) -> str | None:
    if frame is None:
        return "verified_stock_history_missing"
    interval = frame.filter((pl.col("ts") >= origin) & (pl.col("ts") <= expiry))
    if tuple(interval["ts"]) != calendar.sessions(origin, expiry):
        return "verified_stock_interval_missing"
    for column in ("dividends", "stock_splits"):
        if interval.filter(
            pl.col(column).is_null() | ~pl.col(column).is_finite() | (pl.col(column) < 0)
        ).height:
            return "corporate_action_metadata_invalid"
    # A split later than expiry can still change the frozen Yahoo historical price basis.
    if frame.filter((pl.col("ts") >= origin) & (pl.col("stock_splits") > 0)).height:
        return "unverifiable_split_price_basis"
    if interval.filter((pl.col("ts") > origin) & (pl.col("dividends") > 0)).height:
        return "dividend_in_outcome_interval"
    return None


def _finite(value) -> float | None:
    return float(value) if value is not None and isfinite(float(value)) else None


def run_calibration(days: pl.DataFrame, stocks: pl.DataFrame, output: Path) -> dict:
    """Write one attempted model row per observed exact-horizon contract.

    Reconciliation failures remain scored in the inclusion sensitivity, with
    primary_eligible=False. Other unsafe inputs remain explicit unavailable rows.
    Summary counters are observations, never independent inference counts.
    """
    destination = output / "forecast_cells.parquet"
    if destination.exists():
        raise FileExistsError("forecast detail destination already exists")
    output.mkdir(parents=True, exist_ok=True)
    calendar = SessionCalendar()
    frames = _frames(stocks)
    forecaster = PredictiveForecaster(output, calendar=calendar)
    forecaster.prices = FrozenPrices(frames)
    shadow = PhysicalShadowForecaster(forecaster, enforce_fit_latency=False)
    stock_closes = {
        (ticker, row["ts"]): float(row["close"])
        for ticker, frame in frames.items() for row in frame.iter_rows(named=True)
        if row["close"] is not None and isfinite(row["close"]) and row["close"] > 0
    }
    counts: Counter = Counter()
    statuses: Counter = Counter()
    reasons: Counter = Counter()
    availability: Counter = Counter()
    quality: Counter = Counter()
    invalid_scores: Counter = Counter()
    buffer: list[dict] = []
    # Explicit schemas also make empty archives reproducible and readable.
    with pq.ParquetWriter(destination, FORECAST_SCHEMA, compression="zstd") as writer:
        def emit(row: dict) -> None:
            buffer.append(row)
            statuses[(row["model"], row["status"])] += 1
            availability[(row["model"], "available" if row["forecast_available"]
                          else "unavailable")] += 1
            quality[(row["panel"], "primary_eligible" if row["primary_eligible"]
                     else "excluded_from_primary")] += 1
            if row.get("reason"):
                reasons[row["reason"]] += 1
            if row.get("score_reason"):
                invalid_scores[(row["model"], row["score_reason"])] += 1
            if len(buffer) >= 8192:
                writer.write_table(pa.Table.from_pylist(buffer, schema=FORECAST_SCHEMA))
                buffer.clear()

        groups = days.sort(["ticker", "session", "expiry", "contract"]).partition_by(
            ["ticker", "session"], maintain_order=True
        )
        for origin_days in groups:
            ticker = origin_days["ticker"][0]
            origin = origin_days["session"][0]
            decision_close = stock_closes.get((ticker, origin))
            frame = frames.get(ticker)
            if origin is None or not calendar.exchange.is_session(origin):
                counts["invalid_non_session_contract_days"] += origin_days.height
                continue
            as_of = calendar.exchange.session_close(origin).to_pydatetime() + timedelta(seconds=1)
            for expiry_days in origin_days.partition_by("expiry", maintain_order=True):
                expiry = expiry_days["expiry"][0]
                if expiry is None:
                    counts["invalid_expiry_contract_days"] += expiry_days.height
                    continue
                maturity = calendar.expiry_session(expiry)
                horizon = calendar.horizon(origin, expiry)
                counts["observed_contract_days"] += expiry_days.height
                if not 1 <= horizon <= 25:
                    counts["unsupported_or_completed_horizon"] += expiry_days.height
                    continue
                counts["exact_horizon_contract_days"] += expiry_days.height
                panel = panel_for(ticker, origin, maturity)
                maturity_close = stock_closes.get((ticker, maturity))
                action_reason = _action_reason(frame, origin, maturity, calendar)
                forecasts = shadow.forecast_candidates(ticker, as_of, expiry)
                # Models needing uncollected qualified inputs are listed, never provisioned.
                for model in MODELS:
                    forecast = forecasts[model]
                    distribution = forecast.distribution
                    integrator = None
                    distribution_reason = forecast.reason
                    score_crps = None
                    if distribution is not None:
                        try:
                            integrator = WeightedPayoffIntegrator(
                                distribution.prices, distribution.weights
                            )
                            if maturity_close is not None:
                                score_crps = _finite(integrator.crps(maturity_close))
                                if score_crps is not None and score_crps < 0:
                                    score_crps = None
                        except ValueError:
                            distribution_reason = "invalid_scenario_distribution"
                    for side in ("call", "put"):
                        side_days = expiry_days.filter(pl.col("side") == side)
                        if side_days.is_empty():
                            continue
                        entries = list(side_days.iter_rows(named=True))
                        strikes = np.asarray(side_days["strike"], dtype=float)
                        premiums = np.asarray(side_days["mark"], dtype=float)
                        valid_inputs = np.isfinite(strikes) & (strikes > 0) & np.isfinite(premiums)
                        safe_strikes = np.where(valid_inputs, strikes, 1.0)
                        safe_premiums = np.where(valid_inputs, premiums, 0.0)
                        integrated = None
                        probabilities = None
                        if integrator is not None and decision_close is not None:
                            # Baseline probability is its native analytic lognormal CDF.
                            probabilities = (
                                np.asarray([
                                    distribution.probability(side, k) for k in safe_strikes
                                ])
                                if model == "lognormal_ewma"
                                else integrator.strict_itm(side, safe_strikes)
                            )
                            try:
                                integrated = integrator.integrate(
                                    side, safe_strikes, safe_premiums, decision_close
                                )
                            except ValueError:
                                pass  # Invalid source rows are still recorded below.
                        for index, entry in enumerate(entries):
                            row = {name: None for name in _FIELDS}
                            reconciliation = bool(entry.get("reconciliation_failed", False))
                            input_reason = (
                                (entry.get("reason") or "invalid_decision_row")
                                if not entry.get("valid", True) else None
                            )
                            if input_reason is None and (
                                not isfinite(entry["strike"]) or entry["strike"] <= 0
                                or entry["mark"] is None or not isfinite(entry["mark"])
                                or entry["mark"] <= 0 or entry["volume"] is None
                                or not isfinite(entry["volume"]) or entry["volume"] <= 0
                            ):
                                input_reason = "invalid_decision_mark_or_activity"
                            row.update(
                                panel=panel, ticker=ticker, origin=origin, expiry=expiry,
                                expiry_session=maturity, contract=entry["contract"], side=side,
                                horizon=horizon, strike=entry["strike"],
                                decision_close=decision_close, premium_mark=entry["mark"],
                                model=model, status="unavailable",
                                model_version=distribution.model_version if distribution else None,
                                reason=input_reason or distribution_reason,
                                primary_eligible=not reconciliation and not input_reason
                                and not action_reason and decision_close is not None,
                                reconciliation_failed=reconciliation,
                                forecast_available=False,
                                forecast_anchor=distribution.spot if distribution else None,
                                distribution_support=distribution.support if distribution else None,
                            )
                            if decision_close is None:
                                row["reason"] = "verified_decision_close_missing"
                            if row["reason"] or integrator is None or integrated is None:
                                emit(row)
                                continue
                            probability = _finite(probabilities[index])
                            if probability is None or not 0 <= probability <= 1:
                                row["reason"] = "invalid_probability"
                                emit(row)
                                continue
                            row["prediction"] = probability
                            row["forecast_available"] = True
                            row["expected_pnl"] = _finite(integrated.expected_pnl[index])
                            row["loss_probability"] = _finite(integrated.loss_probability[index])
                            row["quantile05"] = _finite(integrated.quantile05[index])
                            row["atom"] = _finite(integrated.cap_atom[index])
                            row["quantile_atom"] = _finite(integrated.quantile_atom[index])
                            if action_reason is not None:
                                row.update(status="outcome_unavailable", reason=action_reason)
                                emit(row)
                                continue
                            if maturity_close is None:
                                row.update(
                                    status="outcome_unavailable", reason="maturity_close_missing"
                                )
                                emit(row)
                                continue
                            strike = entry["strike"]
                            observed_itm = (
                                maturity_close > strike
                                if side == "call" else maturity_close < strike
                            )
                            realized = (
                                min(maturity_close, strike) - decision_close + entry["mark"]
                                if side == "call"
                                else entry["mark"] - max(strike - maturity_close, 0)
                            )
                            clipped = min(max(probability, 1e-6), 1 - 1e-6)
                            row.update(
                                itm=observed_itm, atm=maturity_close == strike,
                                brier=(probability - int(observed_itm)) ** 2,
                                log_loss=-log(clipped if observed_itm else 1 - clipped),
                                crps=score_crps,
                                normalized_crps=_finite(score_crps / decision_close)
                                if score_crps is not None else None,
                                realized_pnl=_finite(realized), realized_loss=realized < 0,
                                status="scored",
                            )
                            if row["quantile05"] is not None:
                                row.update(
                                    pinball=_finite(pinball_loss(realized, row["quantile05"])),
                                    breach_strict=realized < row["quantile05"],
                                    breach_inclusive=realized <= row["quantile05"],
                                )
                            invalid = [
                                metric for metric in (
                                    "crps", "normalized_crps", "expected_pnl", "realized_pnl",
                                    "loss_probability", "quantile05", "pinball",
                                ) if row[metric] is None
                            ]
                            if invalid:
                                row.update(
                                    status="partially_scored",
                                    score_reason="invalid:" + ",".join(invalid),
                                )
                            emit(row)
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer, schema=FORECAST_SCHEMA))
    return {
        "counts": dict(sorted(counts.items())),
        "model_status_counts": {
            f"{model}:{status}": value for (model, status), value in sorted(statuses.items())
        },
        "model_availability_counts": {
            f"{model}:{status}": value for (model, status), value in sorted(availability.items())
        },
        "panel_quality_counts": {
            f"{panel}:{status}": value for (panel, status), value in sorted(quality.items())
        },
        "invalid_score_counts": {
            f"{model}:{reason}": value for (model, reason), value in sorted(invalid_scores.items())
        },
        "reasons": dict(sorted(reasons.items())),
        "additional_input_methods": {
            "ngboost_pooled": "qualified_training_inputs_unavailable",
            "earnings_jump": "qualified_event_inputs_unavailable",
            "iv_physical": "historical_implied_volatility_inputs_unavailable",
            "intraday_shadow": "qualified_underlying_intraday_inputs_unavailable",
        },
        "availability": "offline numerical validity; elapsed fit latency not enforced",
        "payoff_units": "per share, before costs, decision Close and decision option closing mark",
    }
