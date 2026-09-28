"""Causal physical distributions and their cached live lookup.

Each valid method is available for comparison. HTTP lookup never fits a model.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from math import exp, isfinite, log, pi, sqrt
from time import perf_counter

import numpy as np
import polars as pl
from scipy.optimize import nnls
from scipy.stats import t as student_t

from stocksweeper.forecast.market import clean_completed, price_hash
from stocksweeper.forecast.pooled_ngboost import (
    VERSION as NGBOOST_VERSION,
    load_pooled_model,
    predict_pooled_params,
)
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
from stocksweeper.forecast.provenance import SourceRightsUnverified

SCENARIOS = 4096
MAX_HORIZON = 25
STUDENT_VERSION = "student-t-ewma60-shadow-v2"
GJR_VERSION = "gjr-garch11-t-shadow-v1"
EMPIRICAL_SHADOW_VERSION = "empirical-ewma60-shadow-v1"
HAR_VERSION = "ohlc-har-proxy-v1"
SKEW_T_VERSION = "skew-t-ewma60-v1"
EGARCH_VERSION = "egarch11-skewt-v1"
MARKOV_VERSION = "markov-switching-variance-v1"
NEW_METHODS = ("ohlc_har", "skew_t_ewma", "egarch_skew_t", "markov_switching", "ngboost_pooled")
_MAX_FIT_CACHE = 512
_T_FIT_LIMIT_MS = 2000.0
_GJR_FIT_LIMIT_MS = 5000.0
_NEW_FIT_LIMIT_MS = 5000.0


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
    har_parameters: tuple[float, ...] | None = None
    har_reason: str | None = None
    har_fit_ms: float = 0.0
    skew_parameters: tuple[float, ...] | None = None
    skew_reason: str | None = None
    skew_fit_ms: float = 0.0
    egarch_parameters: tuple[float, ...] | None = None
    egarch_reason: str | None = None
    egarch_fit_ms: float = 0.0
    markov_parameters: tuple[float, ...] | None = None
    markov_reason: str | None = None
    markov_fit_ms: float = 0.0


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
    except (FloatingPointError, ValueError, RuntimeError, np.linalg.LinAlgError):
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
    except (
        FloatingPointError, KeyError, ValueError, RuntimeError, IndexError,
        np.linalg.LinAlgError,
    ):
        return None, "gjr_fit_failed"
    values = (omega, alpha, gamma, beta, nu, last_variance)
    if (
        not all(map(isfinite, values))
        or omega <= 0
        or alpha < 0
        or beta < 0
        # arch's optimizer can return a boundary value a few ulps below zero.
        # Reject a material violation, but normalize harmless numerical noise.
        or alpha + gamma < -1e-10
        or alpha + gamma / 2 + beta >= 1
        or nu <= 2.05
        or last_variance <= 0
    ):
        return None, "gjr_parameters_invalid"
    if alpha + gamma < 0:
        gamma = -alpha
    return (omega, alpha, gamma, beta, nu, last_variance), None


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
    if not np.isfinite(total).all():
        raise ValueError("student scenarios invalid")
    # The unbounded t tail has no finite price mean after exponentiation.
    total = np.clip(total, -log(100), log(100))
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


def _fit_har(frame: pl.DataFrame) -> tuple[tuple[float, ...] | None, str | None]:
    """Fit next-day variance to lagged 1/5/22 daily OHLC variance proxies."""
    splits = np.asarray(frame["stock_splits"].to_list(), dtype=float)
    split_index = np.flatnonzero(splits > 0)
    if split_index.size:
        frame = frame.slice(int(split_index[-1]))
    if frame.height < 224:
        return None, "har_history_short"
    opens = np.asarray(frame["open"].to_list(), dtype=float)
    highs = np.asarray(frame["high"].to_list(), dtype=float)
    lows = np.asarray(frame["low"].to_list(), dtype=float)
    closes = np.asarray(frame["close"].to_list(), dtype=float)
    overnight = np.log(opens[1:] / closes[:-1])
    daily = overnight**2 + np.log(highs[1:] / lows[1:]) ** 2 / (4 * log(2))
    if not np.isfinite(daily).all() or np.any(daily < 0) or np.mean(daily) <= 1e-12:
        return None, "har_inputs_invalid"
    predictors = np.asarray(
        [
            [1.0, daily[t - 1], daily[t - 5 : t].mean(), daily[t - 22 : t].mean()]
            for t in range(22, len(daily))
        ]
    )
    targets = daily[22:]
    if len(targets) < 180:
        return None, "har_history_short"
    try:
        coefficients, _ = nnls(predictors, targets)
    except (FloatingPointError, ValueError, RuntimeError, np.linalg.LinAlgError):
        return None, "har_fit_failed"
    if not np.isfinite(coefficients).all() or coefficients.sum() <= 0:
        return None, "har_parameters_invalid"
    return tuple(map(float, (*coefficients, *daily[-22:]))), None


def _har_terminal(
    spot: float, horizon: int, parameters: tuple[float, ...], seed: int
) -> tuple[float, ...]:
    coefficients = np.asarray(parameters[:4])
    lagged = list(parameters[4:])
    if len(lagged) != 22:
        raise ValueError("invalid HAR lags")
    rng = np.random.default_rng(seed)
    total = np.zeros(SCENARIOS)
    for _ in range(horizon):
        variance = float(
            coefficients @ np.array([1.0, lagged[-1], np.mean(lagged[-5:]), np.mean(lagged)])
        )
        if not isfinite(variance) or variance <= 0:
            raise ValueError("invalid HAR variance")
        total += rng.standard_normal(SCENARIOS) * sqrt(variance)
        lagged.pop(0)
        lagged.append(variance)
    return _terminal_prices(spot, total)


def _fit_arch_skew(
    returns: np.ndarray, split_days: np.ndarray, *, egarch: bool
) -> tuple[tuple[float, ...] | None, str | None]:
    label = "egarch" if egarch else "skew_ewma"
    minimum = 500 if egarch else 180
    if split_days.any():
        returns = returns[int(np.flatnonzero(split_days)[-1]) + 1 :]
    if len(returns) < minimum:
        return (
            None,
            f"{label}_split_safe_history_short" if split_days.any() else f"{label}_history_short",
        )
    try:
        from arch.univariate import EGARCH, EWMAVariance, SkewStudent, ZeroMean
    except ImportError:
        return None, "research_dependency_unavailable"
    try:
        model = ZeroMean(returns * 100)
        model.volatility = EGARCH(p=1, o=1, q=1) if egarch else EWMAVariance(_EWMA_DECAY)
        model.distribution = SkewStudent()
        result = model.fit(disp="off", show_warning=False, options={"maxiter": 250})
        if result.convergence_flag != 0:
            return None, f"{label}_nonconverged"
        params = result.params
        eta, skew = float(params["eta"]), float(params["lambda"])
        last_variance = float(result.conditional_volatility[-1]) ** 2
        if egarch:
            omega = float(params["omega"])
            alpha = float(params["alpha[1]"])
            gamma = float(params["gamma[1]"])
            beta = float(params["beta[1]"])
            last_z = float(returns[-1] * 100 / sqrt(last_variance))
            values = (omega, alpha, gamma, beta, eta, skew, last_variance, last_z)
        else:
            values = (eta, skew, last_variance)
    except (
        FloatingPointError, KeyError, ValueError, RuntimeError, IndexError,
        np.linalg.LinAlgError,
    ):
        return None, f"{label}_fit_failed"
    if (
        not all(map(isfinite, values))
        or eta <= 2.05
        or eta > 300
        or abs(skew) >= 0.99
        or last_variance <= 0
        or (egarch and (beta < 0 or beta >= 1))
    ):
        return None, f"{label}_parameters_invalid"
    return values, None


def _skew_terminal(
    spot: float, horizon: int, parameters: tuple[float, ...], last_return: float, seed: int
) -> tuple[float, ...]:
    from arch.univariate import SkewStudent

    eta, skew, last_variance = parameters
    rng = np.random.default_rng(seed)
    draw = SkewStudent(seed=rng).simulate([eta, skew])
    variance = np.full(
        SCENARIOS, _EWMA_DECAY * last_variance + (1 - _EWMA_DECAY) * (last_return * 100) ** 2
    )
    total = np.zeros(SCENARIOS)
    for _ in range(horizon):
        realized = np.sqrt(variance) * draw(SCENARIOS)
        total += realized / 100
        variance = _EWMA_DECAY * variance + (1 - _EWMA_DECAY) * realized**2
    return _terminal_prices(spot, total)


def _egarch_terminal(
    spot: float, horizon: int, parameters: tuple[float, ...], seed: int
) -> tuple[float, ...]:
    from arch.univariate import SkewStudent

    omega, alpha, gamma, beta, eta, skew, last_variance, last_z = parameters
    rng = np.random.default_rng(seed)
    draw = SkewStudent(seed=rng).simulate([eta, skew])
    log_variance = np.full(SCENARIOS, log(last_variance))
    previous_z = np.full(SCENARIOS, last_z)
    total = np.zeros(SCENARIOS)
    for _ in range(horizon):
        log_variance = (
            omega
            + alpha * (np.abs(previous_z) - sqrt(2 / pi))
            + gamma * previous_z
            + beta * log_variance
        )
        if not np.isfinite(log_variance).all() or np.any(np.abs(log_variance) > 50):
            raise ValueError("invalid EGARCH variance")
        previous_z = draw(SCENARIOS)
        total += np.exp(log_variance / 2) * previous_z / 100
    return _terminal_prices(spot, total)


def _fit_markov(
    returns: np.ndarray, split_days: np.ndarray
) -> tuple[tuple[float, ...] | None, str | None]:
    if split_days.any():
        returns = returns[int(np.flatnonzero(split_days)[-1]) + 1 :]
    if len(returns) < 500:
        return (
            None,
            "markov_split_safe_history_short" if split_days.any() else "markov_history_short",
        )
    try:
        from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression
    except ImportError:
        return None, "research_dependency_unavailable"
    try:
        result = MarkovRegression(
            returns * 100, k_regimes=2, trend="n", switching_variance=True
        ).fit(disp=False, em_iter=5, maxiter=100, search_reps=0)
        if not result.mle_retvals.get("converged", False):
            return None, "markov_nonconverged"
        named = dict(zip(result.model.param_names, result.params, strict=True))
        probabilities = np.asarray(result.filtered_marginal_probabilities[-1], dtype=float)
        values = tuple(
            map(
                float,
                (
                    named["p[0->0]"],
                    named["p[1->0]"],
                    named["sigma2[0]"],
                    named["sigma2[1]"],
                    probabilities[0],
                    probabilities[1],
                ),
            )
        )
    except (
        FloatingPointError, KeyError, ValueError, RuntimeError, IndexError,
        np.linalg.LinAlgError,
    ):
        return None, "markov_fit_failed"
    p00, p10, var0, var1, p0, p1 = values
    if (
        not all(map(isfinite, values))
        or not 0 <= p00 <= 1
        or not 0 <= p10 <= 1
        or var0 <= 0
        or var1 <= 0
        or not 0 <= p0 <= 1
        or not 0 <= p1 <= 1
        or abs(p0 + p1 - 1) > 1e-6
    ):
        return None, "markov_parameters_invalid"
    return values, None


def _markov_terminal(
    spot: float, horizon: int, parameters: tuple[float, ...], seed: int
) -> tuple[float, ...]:
    p00, p10, var0, var1, p0, _ = parameters
    rng = np.random.default_rng(seed)
    regime0 = rng.random(SCENARIOS) < p0
    total = np.zeros(SCENARIOS)
    for _ in range(horizon):
        regime0 = rng.random(SCENARIOS) < np.where(regime0, p00, p10)
        total += rng.standard_normal(SCENARIOS) * np.sqrt(np.where(regime0, var0, var1)) / 100
    return _terminal_prices(spot, total)


def _terminal_prices(spot: float, log_returns: np.ndarray) -> tuple[float, ...]:
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        prices = spot * np.exp(log_returns)
    if not np.isfinite(prices).all() or np.any(prices <= 0):
        raise ValueError("candidate scenarios invalid")
    return tuple(sorted(prices.tolist()))


class PhysicalShadowForecaster:
    """Fit once per verified ticker/session without fetching on lookup."""

    def __init__(self, forecaster: PredictiveForecaster) -> None:
        self.forecaster = forecaster
        self._fits: OrderedDict[tuple[str, date, str], _Fits] = OrderedDict()
        self._pooled_artifact: dict[str, object] | None = None
        self._pooled_reason: str | None = None
        self._pooled_checked = False

    def _pooled_model(self) -> tuple[dict[str, object] | None, str | None]:
        if not self._pooled_checked:
            self._pooled_checked = True
            try:
                load_pooled_model(self.forecaster.data_dir)
                # The issuance ledger cannot yet identify which trained artifact produced a result.
                self._pooled_reason = "training_artifact_traceability_unavailable"
            except FileNotFoundError:
                self._pooled_reason = "training_source_rights_unverified"
            except SourceRightsUnverified:
                self._pooled_reason = "training_source_rights_unverified"
            except (OSError, ValueError, TypeError, KeyError):
                self._pooled_reason = "pooled_artifact_invalid"
        return self._pooled_artifact, self._pooled_reason

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
        try:
            import arch  # noqa: F401 - keep the cold import outside the fit latency budget
        except ImportError:
            pass  # _fit_gjr reports the unavailable dependency
        gjr_started = perf_counter()
        gjr_parameters, gjr_reason = _fit_gjr(returns, split_days)
        gjr_ms = (perf_counter() - gjr_started) * 1000
        if gjr_ms > _GJR_FIT_LIMIT_MS:
            gjr_parameters, gjr_reason = None, "gjr_fit_latency_exceeded"
        fits = _Fits(returns, t_parameters, t_reason, gjr_parameters, gjr_reason, t_ms, gjr_ms)
        started_method = perf_counter()
        params, reason = _fit_har(bounded)
        elapsed = (perf_counter() - started_method) * 1000
        fits = replace(
            fits,
            har_parameters=params if elapsed <= _NEW_FIT_LIMIT_MS else None,
            har_reason=reason if elapsed <= _NEW_FIT_LIMIT_MS else "har_fit_latency_exceeded",
            har_fit_ms=elapsed,
        )
        started_method = perf_counter()
        params, reason = _fit_arch_skew(returns, split_days, egarch=False)
        elapsed = (perf_counter() - started_method) * 1000
        fits = replace(
            fits,
            skew_parameters=params if elapsed <= _NEW_FIT_LIMIT_MS else None,
            skew_reason=reason
            if elapsed <= _NEW_FIT_LIMIT_MS
            else "skew_ewma_fit_latency_exceeded",
            skew_fit_ms=elapsed,
        )
        started_method = perf_counter()
        params, reason = _fit_arch_skew(returns, split_days, egarch=True)
        elapsed = (perf_counter() - started_method) * 1000
        fits = replace(
            fits,
            egarch_parameters=params if elapsed <= _NEW_FIT_LIMIT_MS else None,
            egarch_reason=reason if elapsed <= _NEW_FIT_LIMIT_MS else "egarch_fit_latency_exceeded",
            egarch_fit_ms=elapsed,
        )
        started_method = perf_counter()
        params, reason = _fit_markov(returns, split_days)
        elapsed = (perf_counter() - started_method) * 1000
        fits = replace(
            fits,
            markov_parameters=params if elapsed <= _NEW_FIT_LIMIT_MS else None,
            markov_reason=reason if elapsed <= _NEW_FIT_LIMIT_MS else "markov_fit_latency_exceeded",
            markov_fit_ms=elapsed,
        )
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
        if method not in ("empirical_scaled", "student_t_ewma", "gjr_garch_t", *NEW_METHODS):
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
        elif method == "gjr_garch_t":
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
        elif method == "ohlc_har":
            if fits.har_parameters is None:
                return ShadowForecast(None, fits.har_reason, fits.har_fit_ms, 0)
            try:
                prices = _har_terminal(
                    current.spot,
                    horizon,
                    fits.har_parameters,
                    _seed(current.ticker, current.as_of, method, horizon),
                )
            except ValueError:
                return ShadowForecast(None, "candidate_scenarios_invalid", fits.har_fit_ms, 0)
            version, fit_ms = HAR_VERSION, fits.har_fit_ms
        elif method == "skew_t_ewma":
            if fits.skew_parameters is None:
                return ShadowForecast(None, fits.skew_reason, fits.skew_fit_ms, 0)
            try:
                prices = _skew_terminal(
                    current.spot,
                    horizon,
                    fits.skew_parameters,
                    float(fits.returns[-1]),
                    _seed(current.ticker, current.as_of, method, horizon),
                )
            except ValueError:
                return ShadowForecast(None, "candidate_scenarios_invalid", fits.skew_fit_ms, 0)
            version, fit_ms = SKEW_T_VERSION, fits.skew_fit_ms
        elif method == "egarch_skew_t":
            if fits.egarch_parameters is None:
                return ShadowForecast(None, fits.egarch_reason, fits.egarch_fit_ms, 0)
            try:
                prices = _egarch_terminal(
                    current.spot,
                    horizon,
                    fits.egarch_parameters,
                    _seed(current.ticker, current.as_of, method, horizon),
                )
            except ValueError:
                return ShadowForecast(None, "candidate_scenarios_invalid", fits.egarch_fit_ms, 0)
            version, fit_ms = EGARCH_VERSION, fits.egarch_fit_ms
        elif method == "markov_switching":
            if fits.markov_parameters is None:
                return ShadowForecast(None, fits.markov_reason, fits.markov_fit_ms, 0)
            try:
                prices = _markov_terminal(
                    current.spot,
                    horizon,
                    fits.markov_parameters,
                    _seed(current.ticker, current.as_of, method, horizon),
                )
            except ValueError:
                return ShadowForecast(None, "candidate_scenarios_invalid", fits.markov_fit_ms, 0)
            version, fit_ms = MARKOV_VERSION, fits.markov_fit_ms
        else:
            if clean is None or price_hash(clean) != current.data_hash:
                return ShadowForecast(None, "candidate_input_unverified", 0, 0)
            artifact, reason = self._pooled_model()
            if artifact is None:
                return ShadowForecast(None, reason, 0, 0)
            try:
                mean, scale = predict_pooled_params(artifact, clean, horizon, current.as_of)
                rng = np.random.default_rng(_seed(current.ticker, current.as_of, method, horizon))
                prices = _terminal_prices(current.spot, rng.normal(mean, scale, SCENARIOS))
            except ValueError as exc:
                return ShadowForecast(None, str(exc), 0, 0)
            version, fit_ms = NGBOOST_VERSION, 0.0
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
            distribution,
            None,
            fit_ms,
            (perf_counter() - started) * 1000,
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
        names = (
            "lognormal_ewma",
            "empirical_scaled",
            "student_t_ewma",
            "gjr_garch_t",
            *NEW_METHODS,
        )
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
            results.update(
                {
                    name: ShadowForecast(None, "shadow_horizon_unsupported", 0, 0)
                    for name in names[1:]
                }
            )
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
