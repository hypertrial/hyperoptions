"""Small, causal distribution forecast from completed Yahoo Close history.

The baseline is always a zero-log-drift EWMA lognormal distribution when the
latest 61 completed closes are valid. An empirical alternative earns use only
after an out-of-sample, non-overlapping rolling-origin score check.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import exp, isfinite, log, sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Literal

import numpy as np
import polars as pl

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import (
    CacheIntegrityError,
    ForecastPriceStore,
    ForecastProvider,
    YahooForecastProvider,
    clean_completed,
    price_hash,
)

Side = Literal["call", "put"]
_NORMAL = NormalDist()
_EWMA_LOOKBACK = 60
_EWMA_DECAY = 0.94
_EMPIRICAL_MAX_HORIZON = 25
_MIN_INDEPENDENT_BLOCKS = 30
_MAX_CACHED_TICKERS = 512
_SCORE_GRID = (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5)
_BASELINE_QUANTILES = tuple(_NORMAL.inv_cdf((i + 0.5) / 1024) for i in range(1024))
BASELINE_VERSION = "lognormal-ewma60-v1"
EMPIRICAL_VERSION = "empirical-ewma60-rolling-v1"


@dataclass(frozen=True)
class PayoffMetrics:
    """Hypothetical hold-to-expiry P&L per share, before costs or dividends."""

    expected_pnl: float
    loss_probability: float
    fifth_percentile_pnl: float


@dataclass(frozen=True)
class SelectionEvidence:
    independent_blocks: int = 0
    validation_blocks: int = 0
    sample_count: int = 0
    brier_delta: float | None = None
    log_loss_delta: float | None = None
    rejection_reason: str | None = None


@dataclass(frozen=True)
class _ValidationRecord:
    threshold: float
    side: Side
    observed: bool
    baseline: float
    empirical: float


@dataclass(frozen=True)
class _IssuedRecord:
    threshold: float
    side: Side
    observed: bool
    probability: float
    baseline: float
    method: str


@dataclass(frozen=True)
class PredictiveDistribution:
    ticker: str
    status: Literal["available", "unavailable"]
    reason: str | None
    method: Literal["lognormal_ewma", "empirical_scaled"] | None
    as_of: date
    expiry_session: date
    horizon_sessions: int
    spot: float | None = None
    daily_volatility: float | None = None
    model_version: str | None = None
    support: int = 0
    data_hash: str | None = None
    terminal_prices: tuple[float, ...] = ()
    weights: tuple[float, ...] = ()
    selection: SelectionEvidence = SelectionEvidence()

    @property
    def prices(self) -> tuple[float, ...]:
        """Finite, ascending terminal prices for payoff integration."""
        return self.terminal_prices

    def probability(self, side: Side, strike: float | Decimal) -> float | None:
        """Strict expiry ITM: call > strike, put < strike; equality is ATM."""
        strike_value = _positive_price(strike)
        _side(side)
        if self.status != "available":
            return None
        assert self.spot is not None and self.daily_volatility is not None
        if self.method == "lognormal_ewma":
            scaled = log(strike_value / self.spot) / (
                self.daily_volatility * sqrt(self.horizon_sessions)
            )
            return _NORMAL.cdf(-scaled) if side == "call" else _NORMAL.cdf(scaled)
        if side == "call":
            first = bisect_right(self.terminal_prices, strike_value)
            return sum(self.weights[first:])
        last = bisect_left(self.terminal_prices, strike_value)
        return sum(self.weights[:last])

    def payoff_metrics(
        self, side: Side, strike: float | Decimal, premium: float | Decimal
    ) -> PayoffMetrics | None:
        """Covered call or cash-secured short put entered at the stated prices."""
        strike_value = _positive_price(strike)
        premium_value = float(premium)
        _side(side)
        if not isfinite(premium_value) or premium_value < 0:
            raise ValueError("premium must be finite and nonnegative")
        if self.status != "available":
            return None
        assert self.spot is not None
        if side == "call":
            values = [
                min(price, strike_value) - self.spot + premium_value
                for price in self.terminal_prices
            ]
        else:
            values = [
                premium_value - max(strike_value - price, 0.0)
                for price in self.terminal_prices
            ]
        pairs = sorted(zip(values, self.weights, strict=True))
        cumulative = 0.0
        fifth = pairs[-1][0]
        for value, weight in pairs:
            cumulative += weight
            if cumulative >= 0.05:
                fifth = value
                break
        return PayoffMetrics(
            expected_pnl=sum(value * weight for value, weight in pairs),
            loss_probability=sum(weight for value, weight in pairs if value < 0),
            fifth_percentile_pnl=fifth,
        )

    def expected_payoff(
        self, side: Side, strike: float | Decimal, premium: float | Decimal
    ) -> float | None:
        metrics = self.payoff_metrics(side, strike, premium)
        return None if metrics is None else metrics.expected_pnl

    def pnl_quantile(
        self, side: Side, strike: float | Decimal, premium: float | Decimal, q: float
    ) -> float | None:
        if q != 0.05:
            raise ValueError("only the validated fifth-percentile P&L is exposed")
        metrics = self.payoff_metrics(side, strike, premium)
        return None if metrics is None else metrics.fifth_percentile_pnl


def _positive_price(value: float | Decimal) -> float:
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise ValueError("price must be finite and positive")
    return number


def _side(side: str) -> None:
    if side not in ("call", "put"):
        raise ValueError("side must be call or put")


def _ewma_volatility(log_returns: np.ndarray) -> float | None:
    if len(log_returns) != _EWMA_LOOKBACK or not np.isfinite(log_returns).all():
        return None
    weights = _EWMA_DECAY ** np.arange(_EWMA_LOOKBACK - 1, -1, -1)
    variance = float(np.average(log_returns * log_returns, weights=weights))
    return sqrt(variance) if isfinite(variance) and variance > 1e-16 else None


def _score_probability(p: float, observed: bool) -> tuple[float, float]:
    clipped = min(max(p, 1e-6), 1 - 1e-6)
    return (p - observed) ** 2, -log(clipped if observed else 1 - clipped)


def _empirical_probability(sorted_values: list[float], threshold: float) -> float:
    return (len(sorted_values) - bisect_right(sorted_values, threshold)) / len(sorted_values)


def _independent_samples(
    samples: list[tuple[int, int, float]], *, embargo: bool = False
) -> list[tuple[int, int, float]]:
    independent: list[tuple[int, int, float]] = []
    next_origin = -1
    for item in samples:
        if item[0] >= next_origin:
            independent.append(item)
            next_origin = item[1] + int(embargo)
    return independent


def _validation_records(
    samples: list[tuple[int, int, float]],
    independent: list[tuple[int, int, float]],
) -> tuple[list[_ValidationRecord], int]:
    validation = independent[-max(10, len(independent) // 3) :]
    maturities = sorted((maturity, value) for _, maturity, value in samples)
    records: list[_ValidationRecord] = []
    validated_blocks = 0
    for origin, _, outcome in validation:
        training = sorted(value for maturity, value in maturities if maturity < origin)
        if len(training) < 20:
            continue
        validated_blocks += 1
        for threshold in _SCORE_GRID:
            call_empirical = _empirical_probability(training, threshold)
            put_empirical = bisect_left(training, threshold) / len(training)
            records.extend(
                (
                    _ValidationRecord(
                        threshold, "call", outcome > threshold,
                        _NORMAL.cdf(-threshold), call_empirical,
                    ),
                    _ValidationRecord(
                        threshold, "put", outcome < threshold,
                        _NORMAL.cdf(threshold), put_empirical,
                    ),
                )
            )
    return records, validated_blocks


def _empirical_evidence(
    samples: list[tuple[int, int, float]],
) -> tuple[SelectionEvidence, tuple[float, ...] | None]:
    """Score later disjoint horizon blocks using only prior matured labels."""
    independent = _independent_samples(samples)
    blocks = len(independent)
    if blocks < _MIN_INDEPENDENT_BLOCKS:
        return SelectionEvidence(
            independent_blocks=blocks,
            sample_count=len(samples),
            rejection_reason="insufficient_independent_blocks",
        ), None

    records, validated_blocks = _validation_records(
        samples, _independent_samples(samples, embargo=True)
    )
    baseline_brier = baseline_log = empirical_brier = empirical_log = 0.0
    for record in records:
        brier, log_loss = _score_probability(record.baseline, record.observed)
        baseline_brier += brier
        baseline_log += log_loss
        brier, log_loss = _score_probability(record.empirical, record.observed)
        empirical_brier += brier
        empirical_log += log_loss
    if validated_blocks < 10:
        return SelectionEvidence(
            independent_blocks=blocks,
            validation_blocks=validated_blocks,
            sample_count=len(samples),
            rejection_reason="insufficient_validation_blocks",
        ), None
    brier_delta = (empirical_brier - baseline_brier) / len(records)
    log_delta = (empirical_log - baseline_log) / len(records)
    selected = brier_delta < -1e-6 and log_delta <= 1e-12
    evidence = SelectionEvidence(
        independent_blocks=blocks,
        validation_blocks=validated_blocks,
        sample_count=len(samples),
        brier_delta=brier_delta,
        log_loss_delta=log_delta,
        rejection_reason=None if selected else "empirical_score_gate_failed",
    )
    return evidence, tuple(sorted(value for _, _, value in samples)) if selected else None


def _samples(
    bars: pl.DataFrame, as_of: date, horizon: int, calendar: SessionCalendar,
    *, lookback_days: int = 1096,
) -> list[tuple[int, int, float]]:
    """Matured, volatility-scaled returns from the requested bounded window."""
    sessions = calendar.sessions(bars["ts"][0], as_of)
    index = {day: i for i, day in enumerate(sessions)}
    closes = np.full(len(sessions), np.nan)
    for day, close in bars.select("ts", "close").iter_rows():
        if day in index:
            closes[index[day]] = close
    with np.errstate(divide="ignore", invalid="ignore"):
        daily = np.diff(np.log(closes))
    first_day = as_of - timedelta(days=lookback_days)
    result: list[tuple[int, int, float]] = []
    for origin in range(_EWMA_LOOKBACK, len(sessions) - horizon):
        if sessions[origin] < first_day:
            continue
        volatility = _ewma_volatility(daily[origin - _EWMA_LOOKBACK : origin])
        maturity = origin + horizon
        if volatility is None or not isfinite(closes[maturity]):
            continue
        standardized = log(closes[maturity] / closes[origin]) / (
            volatility * sqrt(horizon)
        )
        if isfinite(standardized):
            result.append((origin, maturity, standardized))
    return result


def _moneyness_bucket(threshold: float) -> str:
    if abs(threshold) <= 0.5:
        return "near_atm"
    return "moderate" if abs(threshold) <= 1.0 else "tail"


def _score_summary(records: list[_IssuedRecord], *, baseline: bool = False) -> dict | None:
    if not records:
        return None
    scores = [
        _score_probability(record.baseline if baseline else record.probability, record.observed)
        for record in records
    ]
    return {
        "count": len(records),
        "brier": sum(item[0] for item in scores) / len(records),
        "log_loss": sum(item[1] for item in scores) / len(records),
    }


def _calibration(records: list[_IssuedRecord]) -> list[dict]:
    bins = [[] for _ in range(5)]
    for record in records:
        bins[min(4, int(record.probability * 5))].append(record)
    return [
        {
            "lower": index / 5,
            "upper": (index + 1) / 5,
            "count": len(group),
            "mean_probability": (
                sum(record.probability for record in group) / len(group) if group else None
            ),
            "observed_rate": (
                sum(record.observed for record in group) / len(group) if group else None
            ),
        }
        for index, group in enumerate(bins)
    ]


def _audit_bucket() -> dict:
    return {
        "attempted": 0,
        "issued": 0,
        "rejection_reasons": Counter(),
        "unscored_reasons": Counter(),
        "records": [],
    }


def _coverage_summary(bucket: dict) -> dict:
    attempted = bucket["attempted"]
    issued = bucket["issued"]
    scored = len(bucket["records"])
    return {
        "attempted": attempted,
        "issued": issued,
        "rejected": attempted - issued,
        "coverage_rate": issued / attempted if attempted else None,
        "scored": scored,
        "unscored": issued - scored,
        "score_coverage_rate": scored / issued if issued else None,
        "rejection_reasons": dict(sorted(bucket["rejection_reasons"].items())),
        "unscored_reasons": dict(sorted(bucket["unscored_reasons"].items())),
    }


def _prior_samples(
    matured: list[tuple[int, int, float]], sessions: tuple[date, ...], origin: int
) -> list[tuple[int, int, float]]:
    lower = sessions[origin] - timedelta(days=1096)
    return [
        sample for sample in matured
        if sessions[sample[0]] >= lower and sample[1] < origin
    ]


def evaluate_predictive_history(
    frame: pl.DataFrame, as_of: date, calendar: SessionCalendar | None = None
) -> dict[str, object]:
    """Causal prequential scores and actual probability forecast coverage.

    Each origin first selects its method from labels matured strictly earlier.
    The scored origins are horizon+1 sessions apart, with an embargoed session
    after each maturity. Thus scores/calibration never reuse selection labels.
    """
    calendar = calendar or SessionCalendar()
    clean = clean_completed(frame.filter(pl.col("ts") <= as_of), as_of, calendar)
    if clean.is_empty() or clean["ts"][-1] != as_of:
        raise ValueError("evaluation requires the exact completed Close")
    sessions = calendar.sessions(clean["ts"][0], as_of)
    index = {day: i for i, day in enumerate(sessions)}
    closes = np.full(len(sessions), np.nan)
    for day, close in clean.select("ts", "close").iter_rows():
        closes[index[day]] = close
    with np.errstate(divide="ignore", invalid="ignore"):
        daily = np.diff(np.log(closes))
    window_start = as_of - timedelta(days=1096)
    first_origin = next((i for i, day in enumerate(sessions) if day >= window_start), len(sessions))
    horizons: list[dict[str, object]] = []

    for horizon in range(1, _EMPIRICAL_MAX_HORIZON + 1):
        # Six years are read only so the earliest scored origin can see its
        # own preceding three years. Each candidate fit below is capped at 3y.
        matured = _samples(clean, as_of, horizon, calendar, lookback_days=2192)
        buckets = {name: _audit_bucket() for name in ("all", "near_atm", "moderate", "tail")}
        method_origins: Counter[str] = Counter()
        selection_reasons: Counter[str] = Counter()
        for origin in range(first_origin, len(sessions) - horizon, horizon + 1):
            reason = None
            if not isfinite(closes[origin]):
                reason = "origin_close_missing"
            elif origin < _EWMA_LOOKBACK:
                reason = "ticker_history_short"
            elif not np.isfinite(daily[origin - _EWMA_LOOKBACK : origin]).all():
                reason = "lookback_close_missing"
            else:
                volatility = _ewma_volatility(daily[origin - _EWMA_LOOKBACK : origin])
                if volatility is None:
                    reason = "volatility_unusable"
            empirical: tuple[float, ...] | None = None
            if reason is None:
                prior = _prior_samples(matured, sessions, origin)
                evidence, empirical = _empirical_evidence(prior)
                method = "empirical_scaled" if empirical is not None else "lognormal_ewma"
                method_origins[method] += 1
                if evidence.rejection_reason is not None:
                    selection_reasons[evidence.rejection_reason] += 1
                outcome = (
                    log(closes[origin + horizon] / closes[origin]) /
                    (volatility * sqrt(horizon))
                    if isfinite(closes[origin + horizon]) else None
                )
                empirical_values = list(empirical) if empirical is not None else None
            for threshold in _SCORE_GRID:
                bucket_name = _moneyness_bucket(threshold)
                for side in ("call", "put"):
                    for bucket in (buckets["all"], buckets[bucket_name]):
                        bucket["attempted"] += 1
                        if reason is not None:
                            bucket["rejection_reasons"][reason] += 1
                            continue
                        bucket["issued"] += 1
                        if outcome is None or not isfinite(outcome):
                            bucket["unscored_reasons"]["maturity_close_missing"] += 1
                            continue
                        baseline = _NORMAL.cdf(-threshold if side == "call" else threshold)
                        if empirical_values is None:
                            probability = baseline
                        elif side == "call":
                            probability = _empirical_probability(empirical_values, threshold)
                        else:
                            probability = bisect_left(empirical_values, threshold) / len(
                                empirical_values
                            )
                        bucket["records"].append(_IssuedRecord(
                            threshold, side,
                            outcome > threshold if side == "call" else outcome < threshold,
                            probability, baseline, method,
                        ))
        all_records = buckets["all"]["records"]
        empirical_records = [
            record for record in all_records if record.method == "empirical_scaled"
        ]
        horizons.append({
            "horizon_sessions": horizon,
            "forecast_coverage": _coverage_summary(buckets["all"]),
            "by_moneyness": {
                name: {
                    **_coverage_summary(buckets[name]),
                    "model_scores": _score_summary(buckets[name]["records"]),
                }
                for name in ("near_atm", "moderate", "tail")
            },
            "model_scores": _score_summary(all_records),
            "baseline_comparator": _score_summary(all_records, baseline=True),
            "empirical_selected_origin_scores": _score_summary(empirical_records),
            "baseline_on_empirical_origins": _score_summary(empirical_records, baseline=True),
            "calibration": _calibration(all_records),
            "method_origins": dict(sorted(method_origins.items())),
            "selection_rejection_reasons": dict(sorted(selection_reasons.items())),
        })
    return {
        "ticker_data_hash": price_hash(clean),
        "as_of": as_of.isoformat(),
        "source": "Yahoo Finance daily Close (split-normalized, dividend-unadjusted)",
        "evaluation_design": "causal rolling origins separated by horizon plus one session",
        "coverage_universe": "scheduled origins x seven standardized strikes x call and put",
        "standardized_log_moneyness_grid": list(_SCORE_GRID),
        "moneyness_bins": {
            "near_atm": "absolute standardized log moneyness <= 0.5",
            "moderate": "0.5 < absolute standardized log moneyness <= 1.0",
            "tail": "absolute standardized log moneyness > 1.0",
        },
        "horizons": horizons,
    }


class PredictiveForecaster:
    """Prepare prices out of band; forecast from local verified data only."""

    def __init__(
        self,
        data_dir: Path,
        provider: ForecastProvider | None = None,
        calendar: SessionCalendar | None = None,
    ) -> None:
        self.calendar = calendar or SessionCalendar()
        self.prices = ForecastPriceStore(data_dir, provider or YahooForecastProvider())
        self._prepared: dict[tuple[str, date], pl.DataFrame] = {}
        self._inputs: dict[
            tuple[str, date], tuple[pl.DataFrame, float, float, str] | str
        ] = {}
        self._candidates: dict[
            tuple[str, date, int], tuple[SelectionEvidence, tuple[float, ...] | None]
        ] = {}
        self._ticker_sessions: OrderedDict[str, date] = OrderedDict()

    def _evict_ticker(self, ticker: str) -> None:
        for cache in (self._prepared, self._inputs, self._candidates):
            for key in tuple(cache):
                if key[0] == ticker:
                    del cache[key]

    def _touch_cache(self, ticker: str, completed: date) -> None:
        previous = self._ticker_sessions.pop(ticker, None)
        if previous is not None and previous != completed:
            self._evict_ticker(ticker)
        self._ticker_sessions[ticker] = completed
        while len(self._ticker_sessions) > _MAX_CACHED_TICKERS:
            oldest, _ = self._ticker_sessions.popitem(last=False)
            self._evict_ticker(oldest)

    def prepare(self, ticker: str, completed: date) -> None:
        """Only method that fetches Yahoo; intended for a background coordinator."""
        try:
            frame = self.prices.update(ticker, completed)
        except CacheIntegrityError:
            frame = self.prices.update(ticker, completed, full_refresh=True)
        if frame["ts"][-1] != completed:
            raise ValueError(f"market_data_missing: {ticker} has no {completed} Close")
        # Reading back verifies both the Parquet and manifest before publication.
        verified = self.prices.read(ticker)
        assert verified is not None
        self._touch_cache(ticker, completed)
        self._prepared[(ticker, completed)] = verified
        self._inputs.pop((ticker, completed), None)
        for key in list(self._candidates):
            if key[:2] == (ticker, completed):
                del self._candidates[key]

    def forecast(
        self,
        ticker: str,
        as_of: datetime,
        expiry: date,
        *,
        contract_since: date | None = None,
        standard_terms: bool = True,
    ) -> PredictiveDistribution:
        if as_of.tzinfo is None:
            raise ValueError("as_of must have a timezone")
        completed = self.calendar.last_completed(as_of)
        expiry_session = self.calendar.expiry_session(expiry)
        horizon = self.calendar.horizon(completed, expiry)

        def unavailable(reason: str) -> PredictiveDistribution:
            return PredictiveDistribution(
                ticker, "unavailable", reason, None, completed, expiry_session, horizon
            )

        if not standard_terms:
            return unavailable("contract_terms_ambiguous")
        if horizon < 1:
            return unavailable("expiry_completed")
        try:
            one_year = completed.replace(year=completed.year + 1)
        except ValueError:  # February 29 has no anniversary in a non-leap year.
            one_year = completed.replace(year=completed.year + 1, day=28)
        if expiry_session > one_year:
            return unavailable("horizon_unsupported")
        try:
            self._touch_cache(ticker, completed)
            key = (ticker, completed)
            frame = self._prepared.get(key)
            if frame is None:
                frame = self.prices.read(ticker)
                if frame is None:
                    return unavailable("market_data_missing")
                frame = frame.filter(pl.col("ts") <= completed)
                self._prepared[key] = frame
            if frame.is_empty() or frame["ts"][-1] != completed:
                return unavailable("market_data_missing")
            if contract_since is not None:
                actions = frame.filter(pl.col("ts") >= contract_since)
                expected = self.calendar.sessions(contract_since, completed)
                if len(expected) != actions.height or tuple(actions["ts"]) != expected:
                    return unavailable("contract_terms_ambiguous")
                if actions.filter(
                    pl.col("stock_splits").is_null()
                    | ~pl.col("stock_splits").is_finite()
                    | (pl.col("stock_splits") < 0)
                ).height:
                    return unavailable("contract_terms_ambiguous")
                if actions.filter(pl.col("stock_splits") > 0).height:
                    return unavailable("contract_terms_changed")
            inputs = self._inputs.get(key)
            if inputs is None:
                clean = clean_completed(frame, completed, self.calendar)
                if clean.is_empty() or clean["ts"][-1] != completed:
                    inputs = "market_data_invalid"
                else:
                    last_sessions = self.calendar.sessions(clean["ts"][0], completed)[
                        -(_EWMA_LOOKBACK + 1) :
                    ]
                    recent = clean.filter(pl.col("ts").is_in(last_sessions))
                    if len(last_sessions) != _EWMA_LOOKBACK + 1 or recent.height != len(
                        last_sessions
                    ):
                        inputs = "ticker_history_short"
                    else:
                        closes = np.asarray(recent["close"].to_list(), dtype=float)
                        with np.errstate(divide="ignore", invalid="ignore"):
                            volatility = _ewma_volatility(np.diff(np.log(closes)))
                        if volatility is None:
                            inputs = "volatility_unusable"
                        else:
                            inputs = (
                                clean,
                                float(closes[-1]),
                                volatility,
                                price_hash(clean),
                            )
                self._inputs[key] = inputs
            if isinstance(inputs, str):
                return unavailable(inputs)
            clean, spot, volatility, digest = inputs
            evidence = SelectionEvidence(rejection_reason="horizon_above_empirical_limit")
            empirical = None
            if horizon <= _EMPIRICAL_MAX_HORIZON:
                candidate_key = (ticker, completed, horizon)
                candidate = self._candidates.get(candidate_key)
                if candidate is None:
                    candidate = _empirical_evidence(
                        _samples(clean, completed, horizon, self.calendar)
                    )
                    self._candidates[candidate_key] = candidate
                evidence, empirical = candidate
            if empirical is None:
                terminal = tuple(
                    spot * exp(volatility * sqrt(horizon) * z) for z in _BASELINE_QUANTILES
                )
                method: Literal["lognormal_ewma", "empirical_scaled"] = "lognormal_ewma"
                version = BASELINE_VERSION
                support = _EWMA_LOOKBACK
            else:
                terminal = tuple(
                    spot * exp(volatility * sqrt(horizon) * value) for value in empirical
                )
                method = "empirical_scaled"
                version = EMPIRICAL_VERSION
                support = len(empirical)
            terminal = tuple(sorted(terminal))
            if not all(isfinite(price) and price > 0 for price in terminal):
                return unavailable("volatility_unusable")
            weights = (1.0 / len(terminal),) * len(terminal)
            return PredictiveDistribution(
                ticker=ticker,
                status="available",
                reason=None,
                method=method,
                as_of=completed,
                expiry_session=expiry_session,
                horizon_sessions=horizon,
                spot=spot,
                daily_volatility=volatility,
                model_version=version,
                support=support,
                data_hash=digest,
                terminal_prices=terminal,
                weights=weights,
                selection=evidence,
            )
        except (OSError, ValueError, OverflowError, pl.exceptions.PolarsError):
            return unavailable("market_data_invalid")
