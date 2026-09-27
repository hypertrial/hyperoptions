"""Causal physical distributions and their cached live lookup.

Each valid method is available for comparison. HTTP lookup never fits a model.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from math import exp, isfinite, sqrt
from time import perf_counter

import numpy as np
import polars as pl
from scipy.stats import t as student_t

from stocksweeper.forecast.market import clean_completed, price_hash
from stocksweeper.forecast.predictive import (
    BASELINE_VERSION,
    _BASELINE_QUANTILES,
    _EWMA_DECAY,
    _EWMA_LOOKBACK,
    _independent_samples,
    _samples,
    _ewma_volatility,
    PredictiveDistribution,
    PredictiveForecaster,
)

SCENARIOS = 4096
MAX_HORIZON = 25
STUDENT_VERSION = "student-t-ewma60-shadow-v1"
GJR_VERSION = "gjr-garch11-t-shadow-v1"
EMPIRICAL_SHADOW_VERSION = "empirical-ewma60-shadow-v1"
_MAX_FIT_CACHE = 512
_T_FIT_LIMIT_MS = 2000.0
_GJR_FIT_LIMIT_MS = 5000.0


@dataclass(frozen=True)
class ShadowForecast:
    distribution: PredictiveDistribution | None
    reason: str | None
    prepare_ms: float
    lookup_ms: float
    independent_blocks: int | None = None


@dataclass(frozen=True)
class _Fits:
    returns: np.ndarray
    t_parameters: tuple[float, float] | None
    t_reason: str | None
    gjr_parameters: tuple[float, float, float, float, float, float] | None
    gjr_reason: str | None
    t_fit_ms: float
    gjr_fit_ms: float


def _seed(ticker: str, as_of: date, method: str, horizon: int) -> int:
    digest = hashlib.sha256(f"{ticker}|{as_of}|{method}|{horizon}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _t_innovations(returns: np.ndarray, split_days: np.ndarray) -> np.ndarray:
    values: list[float] = []
    for index in range(_EWMA_LOOKBACK, len(returns)):
        if split_days[index - _EWMA_LOOKBACK : index + 1].any():
            continue
        volatility = _ewma_volatility(returns[index - _EWMA_LOOKBACK : index])
        if volatility is not None:
            values.append(float(returns[index] / volatility))
    return np.asarray(values)


def _fit_student(
    returns: np.ndarray, split_days: np.ndarray
) -> tuple[tuple[float, float] | None, str | None]:
    innovations = _t_innovations(returns, split_days)
    if len(innovations) < 60:
        return None, "student_history_short"
    try:
        degrees, _, scale = student_t.fit(innovations, floc=0)
    except (FloatingPointError, ValueError, RuntimeError):
        return None, "student_fit_failed"
    if not all(map(isfinite, (degrees, scale))) or not 2.05 < degrees <= 200 or scale <= 0:
        return None, "student_parameters_invalid"
    return (float(degrees), float(scale)), None


def _fit_gjr(
    returns: np.ndarray, split_days: np.ndarray
) -> tuple[tuple[float, float, float, float, float, float] | None, str | None]:
    if len(returns) < 500:
        return None, "gjr_history_short"
    if split_days.any():
        return None, "gjr_split_in_window"
    try:
        from arch import arch_model
    except ImportError:
        return None, "research_dependency_unavailable"
    try:
        result = arch_model(
            returns * 100,
            mean="Zero",
            vol="GARCH",
            p=1,
            o=1,
            q=1,
            dist="StudentsT",
            rescale=False,
        ).fit(disp="off", show_warning=False, options={"maxiter": 250})
        if result.convergence_flag != 0:
            return None, "gjr_nonconverged"
        parameters = result.params
        omega = float(parameters["omega"])
        alpha = float(parameters["alpha[1]"])
        gamma = float(parameters["gamma[1]"])
        beta = float(parameters["beta[1]"])
        nu = float(parameters["nu"])
        last_variance = float(result.conditional_volatility[-1]) ** 2
    except (FloatingPointError, KeyError, ValueError, RuntimeError, IndexError):
        return None, "gjr_fit_failed"
    values = (omega, alpha, gamma, beta, nu, last_variance)
    if (
        not all(map(isfinite, values))
        or omega <= 0
        or alpha < 0
        or beta < 0
        or alpha + gamma < 0
        or alpha + gamma / 2 + beta >= 1
        or nu <= 2.05
        or last_variance <= 0
    ):
        return None, "gjr_parameters_invalid"
    return values, None


def _student_terminal(
    spot: float, volatility: float, horizon: int, parameters: tuple[float, float], seed: int
) -> tuple[float, ...]:
    degrees, scale = parameters
    rng = np.random.default_rng(seed)
    daily = np.full(SCENARIOS, volatility)
    total = np.zeros(SCENARIOS)
    for _ in range(horizon):
        shock = rng.standard_t(degrees, SCENARIOS) * scale
        realized = daily * shock
        total += realized
        daily = np.sqrt(_EWMA_DECAY * daily**2 + (1 - _EWMA_DECAY) * realized**2)
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        terminal = spot * np.exp(total)
    if not np.isfinite(terminal).all() or np.any(terminal <= 0):
        raise ValueError("student scenarios invalid")
    return tuple(sorted(terminal.tolist()))


def _gjr_terminal(
    spot: float,
    horizon: int,
    parameters: tuple[float, float, float, float, float, float],
    last_return: float,
    seed: int,
) -> tuple[float, ...]:
    omega, alpha, gamma, beta, nu, last_variance = parameters
    rng = np.random.default_rng(seed)
    last_percent = last_return * 100
    variance = np.full(
        SCENARIOS,
        omega + (alpha + gamma * (last_percent < 0)) * last_percent**2 + beta * last_variance,
    )
    total = np.zeros(SCENARIOS)
    for _ in range(horizon):
        shock = rng.standard_t(nu, SCENARIOS) * sqrt((nu - 2) / nu)
        realized = np.sqrt(variance) * shock
        total += realized / 100
        variance = omega + (alpha + gamma * (realized < 0)) * realized**2 + beta * variance
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        terminal = spot * np.exp(total)
    if not np.isfinite(terminal).all() or np.any(terminal <= 0):
        raise ValueError("gjr scenarios invalid")
    return tuple(sorted(terminal.tolist()))


class PhysicalShadowForecaster:
    """Fit once per verified ticker/session without fetching on lookup."""

    def __init__(self, forecaster: PredictiveForecaster) -> None:
        self.forecaster = forecaster
        self._fits: OrderedDict[tuple[str, date, str], _Fits] = OrderedDict()

    def _fit(
        self, ticker: str, as_of: date, frame: pl.DataFrame, digest: str
    ) -> tuple[_Fits, float]:
        key = (ticker, as_of, digest)
        cached = self._fits.get(key)
        if cached is not None:
            self._fits.move_to_end(key)
            return cached, 0.0
        started = perf_counter()
        bounded = frame.filter(pl.col("ts") >= as_of - timedelta(days=1096))
        sessions = self.forecaster.calendar.sessions(bounded["ts"][0], as_of)
        if tuple(bounded["ts"]) != sessions:
            empty = _Fits(
                np.array([]), None, "history_bar_missing", None, "history_bar_missing", 0, 0
            )
            self._fits[key] = empty
            return empty, 0.0
        closes = np.asarray(bounded["close"].to_list(), dtype=float)
        returns = np.diff(np.log(closes))
        split_days = np.asarray(bounded["stock_splits"].to_list()[1:], dtype=float) > 0
        t_started = perf_counter()
        t_parameters, t_reason = _fit_student(returns, split_days)
        t_ms = (perf_counter() - t_started) * 1000
        if t_ms > _T_FIT_LIMIT_MS:
            t_parameters, t_reason = None, "student_fit_latency_exceeded"
        gjr_started = perf_counter()
        gjr_parameters, gjr_reason = _fit_gjr(returns, split_days)
        gjr_ms = (perf_counter() - gjr_started) * 1000
        if gjr_ms > _GJR_FIT_LIMIT_MS:
            gjr_parameters, gjr_reason = None, "gjr_fit_latency_exceeded"
        fits = _Fits(returns, t_parameters, t_reason, gjr_parameters, gjr_reason, t_ms, gjr_ms)
        self._fits[key] = fits
        while len(self._fits) > _MAX_FIT_CACHE:
            self._fits.popitem(last=False)
        return fits, (perf_counter() - started) * 1000

    def cached_candidate(
        self,
        current: PredictiveDistribution,
        method: str,
        *,
        clean: pl.DataFrame | None = None,
    ) -> ShadowForecast:
        """Build only one candidate from an existing fit; never optimize here."""
        started = perf_counter()
        if method not in ("empirical_scaled", "student_t_ewma", "gjr_garch_t"):
            raise ValueError("unknown physical challenger")
        key = (current.ticker, current.as_of, current.data_hash)
        fits = self._fits.get(key)
        if fits is None:
            return ShadowForecast(None, "candidate_not_prepared", 0, 0)
        if current.status != "available" or current.horizon_sessions > MAX_HORIZON:
            return ShadowForecast(None, "candidate_horizon_unsupported", 0, 0)
        assert current.spot is not None and current.daily_volatility is not None
        horizon = current.horizon_sessions
        if method == "empirical_scaled":
            if clean is None or price_hash(clean) != current.data_hash:
                return ShadowForecast(None, "candidate_input_unverified", 0, 0)
            samples = _samples(clean, current.as_of, horizon, self.forecaster.calendar)
            independent_blocks = len(_independent_samples(samples))
            if independent_blocks < 30:
                return ShadowForecast(
                    None, "insufficient_independent_blocks", 0, 0, independent_blocks
                )
            values = sorted(value for _, _, value in samples)
            try:
                prices = tuple(
                    sorted(
                        current.spot * exp(current.daily_volatility * sqrt(horizon) * value)
                        for value in values
                    )
                )
            except OverflowError:
                return ShadowForecast(None, "candidate_scenarios_invalid", 0, 0)
            version, fit_ms = EMPIRICAL_SHADOW_VERSION, 0.0
        elif method == "student_t_ewma":
            if fits.t_parameters is None:
                return ShadowForecast(None, fits.t_reason, fits.t_fit_ms, 0)
            try:
                prices = _student_terminal(
                    current.spot,
                    current.daily_volatility,
                    horizon,
                    fits.t_parameters,
                    _seed(current.ticker, current.as_of, method, horizon),
                )
            except ValueError:
                return ShadowForecast(None, "candidate_scenarios_invalid", fits.t_fit_ms, 0)
            version, fit_ms = STUDENT_VERSION, fits.t_fit_ms
        else:
            if fits.gjr_parameters is None:
                return ShadowForecast(None, fits.gjr_reason, fits.gjr_fit_ms, 0)
            try:
                prices = _gjr_terminal(
                    current.spot,
                    horizon,
                    fits.gjr_parameters,
                    float(fits.returns[-1]),
                    _seed(current.ticker, current.as_of, method, horizon),
                )
            except ValueError:
                return ShadowForecast(None, "candidate_scenarios_invalid", fits.gjr_fit_ms, 0)
            version, fit_ms = GJR_VERSION, fits.gjr_fit_ms
        if not prices or not all(isfinite(price) and price > 0 for price in prices):
            return ShadowForecast(None, "candidate_scenarios_invalid", fit_ms, 0)
        distribution = replace(
            current,
            method=method,
            model_version=version,
            support=len(prices),
            terminal_prices=prices,
            weights=(1 / len(prices),) * len(prices),
        )
        return ShadowForecast(
            distribution, None, fit_ms, (perf_counter() - started) * 1000,
            independent_blocks if method == "empirical_scaled" else None,
        )

    def forecast_candidates(
        self,
        ticker: str,
        as_of: datetime,
        expiry: date,
        *,
        contract_since: date | None = None,
        standard_terms: bool = True,
    ) -> dict[str, ShadowForecast]:
        names = ("lognormal_ewma", "empirical_scaled", "student_t_ewma", "gjr_garch_t")
        current = self.forecaster.forecast(
            ticker, as_of, expiry, contract_since=contract_since, standard_terms=standard_terms
        )
        if current.status != "available":
            return {name: ShadowForecast(None, current.reason, 0, 0) for name in names}
        assert current.spot is not None and current.daily_volatility is not None
        horizon = current.horizon_sessions
        started = perf_counter()
        baseline_prices = tuple(
            sorted(
                current.spot * exp(current.daily_volatility * sqrt(horizon) * z)
                for z in _BASELINE_QUANTILES
            )
        )
        baseline = replace(
            current,
            method="lognormal_ewma",
            model_version=BASELINE_VERSION,
            support=_EWMA_LOOKBACK,
            terminal_prices=baseline_prices,
            weights=(1 / len(baseline_prices),) * len(baseline_prices),
        )
        results = {
            "lognormal_ewma": ShadowForecast(baseline, None, 0, (perf_counter() - started) * 1000)
        }
        if horizon > MAX_HORIZON:
            results.update({
                name: ShadowForecast(None, "shadow_horizon_unsupported", 0, 0)
                for name in names[1:]
            })
            return results
        try:
            frame = self.forecaster.prices.read(ticker)
            assert frame is not None
            clean = clean_completed(
                frame.filter(pl.col("ts") <= current.as_of),
                current.as_of,
                self.forecaster.calendar,
            )
            if clean.is_empty() or clean["ts"][-1] != current.as_of:
                raise ValueError("verified completed history missing")
            digest = price_hash(clean)
            if digest != current.data_hash:
                raise ValueError("forecast cache changed during evaluation")
            self._fit(ticker, current.as_of, clean, digest)
        except (OSError, ValueError, pl.exceptions.PolarsError):
            return {
                name: results.get(name, ShadowForecast(None, "market_data_invalid", 0, 0))
                for name in names
            }

        for name in names[1:]:
            results[name] = self.cached_candidate(current, name, clean=clean)
        return results
