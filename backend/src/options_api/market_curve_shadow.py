"""Arbitrage-constrained call-curve comparison for public quotes."""

from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, minimize

from options_api.market_odds import (
    OddsEstimate,
    _Quote,
    _sample_fit_quotes,
    _valid_quote,
    _vertical_bounds_with_reason,
    _years_to_close,
)
from options_api.models import OptionQuote

_MAX_STRIKES = 80
_MAX_FIT_SECONDS = 0.5
_MAX_TOTAL_SECONDS = 5.0
_TICK = 0.01
_MAX_PERTURBATION = 0.03


@dataclass(frozen=True)
class CurveShadowResult:
    odds: dict[tuple[str, Decimal], OddsEstimate]
    held_out_inside: int
    held_out_count: int
    elapsed_ms: float
    rejection_reasons: dict[str, int]
    shadow_held_out_predicted: int = 0
    benchmark_held_out_inside: int = 0
    benchmark_held_out_predicted: int = 0
    paired_held_out_count: int = 0
    paired_shadow_inside: int = 0
    paired_benchmark_inside: int = 0
    held_out_cohort: str = "curve_only"
    expiry_reasons: dict[str, str] = field(default_factory=dict)
    contract_reasons: dict[tuple[str, Decimal], str] = field(default_factory=dict)


def _fit(
    quotes: list[_Quote],
    *,
    shift_index: int | None = None,
    shift: float = 0,
    deadline: float | None = None,
) -> np.ndarray | None:
    if deadline is not None and time.monotonic() >= deadline:
        return None
    n = len(quotes)
    if n < 3 or n > _MAX_STRIKES:
        return None
    strikes = np.asarray([float(q.strike) for q in quotes])
    if np.any(np.diff(strikes) <= 0):
        return None
    discount = math.exp(-quotes[0].rate * quotes[0].years)
    bid = np.asarray([q.bid for q in quotes])
    ask = np.asarray([q.ask for q in quotes])
    mid = (bid + ask) / 2
    if shift_index is not None:
        bid[shift_index] += shift
        ask[shift_index] += shift
        mid[shift_index] += shift
        if bid[shift_index] < 0:
            return None
    spread = np.maximum(ask - bid, _TICK)
    slopes = np.zeros((n - 1, n))
    for i, width in enumerate(np.diff(strikes)):
        slopes[i, i] = -1 / width
        slopes[i, i + 1] = 1 / width
    constraints = [
        LinearConstraint(slopes, -discount, 0),
        LinearConstraint(np.diff(slopes, axis=0), 0, np.inf),
    ]
    started = time.monotonic()

    def objective(prices: np.ndarray) -> float:
        if time.monotonic() - started > _MAX_FIT_SECONDS or (
            deadline is not None and time.monotonic() >= deadline
        ):
            raise TimeoutError("curve fit exceeded time budget")
        error = (prices - mid) / spread
        return float(error @ error)

    def gradient(prices: np.ndarray) -> np.ndarray:
        return 2 * (prices - mid) / (spread * spread)

    try:
        fit = minimize(
            objective,
            mid,
            jac=gradient,
            method="SLSQP",
            bounds=Bounds(bid, ask),
            constraints=constraints,
            options={"maxiter": 100, "ftol": 1e-9},
        )
    except (ArithmeticError, TimeoutError, ValueError):
        return None
    prices = fit.x
    if not fit.success or not np.isfinite(prices).all():
        return None
    if np.any(prices < bid - 1e-6) or np.any(prices > ask + 1e-6):
        return None
    fitted_slopes = slopes @ prices
    if np.any(fitted_slopes < -discount - 1e-6) or np.any(fitted_slopes > 1e-6):
        return None
    if np.any(np.diff(fitted_slopes) < -1e-6):
        return None
    return prices


def _digital(quotes: list[_Quote], prices: np.ndarray, index: int) -> float:
    left = (prices[index] - prices[index - 1]) / float(
        quotes[index].strike - quotes[index - 1].strike
    )
    right = (prices[index + 1] - prices[index]) / float(
        quotes[index + 1].strike - quotes[index].strike
    )
    discount = math.exp(-quotes[index].rate * quotes[index].years)
    return float(-(left + right) / (2 * discount))


def calculate_curve_shadow(
    rows: Sequence[OptionQuote],
    spot: Decimal,
    rate_for_expiry: Callable[[date], float | None],
    allowed_expirations: set[str],
    as_of: datetime,
    benchmark_odds: dict[tuple[str, Decimal], OddsEstimate] | None = None,
) -> CurveShadowResult:
    """Fit each expiry independently; never use this result as published Q."""
    started = time.monotonic()
    deadline = started + _MAX_TOTAL_SECONDS
    try:
        spot_float = float(spot)
    except (TypeError, ValueError, OverflowError):
        spot_float = math.nan
    if not math.isfinite(spot_float) or spot_float <= 0:
        return CurveShadowResult({}, 0, 0, 0, {"invalid_spot": 1})
    by_expiry: dict[str, list[_Quote]] = defaultdict(list)
    rejected: Counter[str] = Counter()
    expiry_reasons: dict[str, str] = {}
    contract_reasons: dict[tuple[str, Decimal], str] = {}
    for row in rows:
        if time.monotonic() >= deadline:
            rejected["shadow_budget_exceeded"] += 1
            break
        if row.expiration not in allowed_expirations:
            continue
        try:
            expiry = date.fromisoformat(row.expiration)
            years = _years_to_close(expiry, as_of)
            rate = rate_for_expiry(expiry)
        except (ArithmeticError, TypeError, ValueError):
            rejected["invalid_expiration_or_rate"] += 1
            contract_reasons[(row.expiration, row.strike)] = "invalid_expiration_or_rate"
            continue
        if years <= 0 or rate is None or not math.isfinite(rate) or not 0 <= rate <= 0.25:
            rejected["invalid_expiration_or_rate"] += 1
            contract_reasons[(row.expiration, row.strike)] = "invalid_expiration_or_rate"
            continue
        quote = _valid_quote(row, years, rate, spot_float)
        if quote is None:
            rejected["invalid_quote_or_terms"] += 1
            contract_reasons[(row.expiration, row.strike)] = "invalid_quote_or_terms"
            continue
        by_expiry[row.expiration].append(quote)
    odds: dict[tuple[str, Decimal], OddsEstimate] = {}
    held_quotes: dict[tuple[str, Decimal], _Quote] = {}
    held_shadow: dict[tuple[str, Decimal], bool] = {}
    _, benchmark_held_out = _sample_fit_quotes(by_expiry, spot_float)
    sampled_keys = {(quote.expiration, quote.strike) for quote in benchmark_held_out}
    issued_keys = {
        key
        for key, estimate in (benchmark_odds or {}).items()
        if estimate.held_out_vanilla_price is not None
    }
    benchmark_keys = issued_keys or sampled_keys
    cohort = (
        "benchmark_as_fitted"
        if issued_keys
        else "benchmark_sampler_unavailable"
        if sampled_keys
        else "curve_only"
    )
    for expiry, quotes in by_expiry.items():
        if time.monotonic() >= deadline:
            rejected["shadow_budget_exceeded"] += len(quotes)
            expiry_reasons[expiry] = "shadow_budget_exceeded"
            continue
        quotes.sort(key=lambda q: q.strike)
        if len({q.strike for q in quotes}) != len(quotes):
            rejected["duplicate_strike"] += len(quotes)
            expiry_reasons[expiry] = "duplicate_strike"
            continue
        if len(quotes) < 5 or len(quotes) > _MAX_STRIKES:
            rejected["sparse_or_large_strip"] += len(quotes)
            expiry_reasons[expiry] = "sparse_or_large_strip"
            continue
        fitted = _fit(quotes, deadline=deadline)
        # The published benchmark already held these keys out of its fit.
        # Single-expiry strips have no benchmark fit and are labeled separately.
        held_indexes = (
            [
                index
                for index, quote in enumerate(quotes)
                if (expiry, quote.strike) in benchmark_keys
            ]
            if benchmark_keys
            else list(range(2, len(quotes) - 2, 5))
        )
        held_set = set(held_indexes)
        train = [q for index, q in enumerate(quotes) if index not in held_set]
        train_fit = _fit(train, deadline=deadline) if held_indexes else None
        if train_fit is None and held_indexes:
            rejected["held_out_curve_fit_failed"] += len(held_indexes)
        training_positions = {quote.strike: index for index, quote in enumerate(train)}
        for index in held_indexes:
            target = quotes[index]
            key = expiry, target.strike
            held_quotes[key] = target
            if train_fit is None:
                continue
            if index == 0 or index == len(quotes) - 1:
                rejected["held_out_bracket_missing"] += 1
                continue
            left = training_positions.get(quotes[index - 1].strike)
            right = training_positions.get(quotes[index + 1].strike)
            if left is None or right is None:
                rejected["held_out_bracket_missing"] += 1
                continue
            weight = float(
                (target.strike - train[left].strike) / (train[right].strike - train[left].strike)
            )
            estimate = train_fit[left] * (1 - weight) + train_fit[right] * weight
            held_shadow[key] = target.bid <= estimate <= target.ask
        if fitted is None:
            rejected["infeasible_curve"] += len(quotes)
            expiry_reasons[expiry] = "infeasible_curve"
            continue
        perturbed_fits: dict[tuple[int, int], np.ndarray | None] = {}
        for index in range(1, len(quotes) - 1):
            target = quotes[index]
            key = expiry, target.strike
            if time.monotonic() >= deadline:
                odds[key] = OddsEstimate(None, "shadow_budget_exceeded")
                continue
            bounds, reason = _vertical_bounds_with_reason(quotes, target)
            if bounds is None:
                odds[key] = OddsEstimate(None, reason)
                continue
            probability = _digital(quotes, fitted, index)
            if not (
                math.isfinite(probability)
                and 0 <= probability <= 1
                and bounds[0] - 1e-6 <= probability <= bounds[1] + 1e-6
            ):
                odds[key] = OddsEstimate(None, "quote_bounds_mismatch")
                continue
            perturbations = []
            for neighboring in (index - 1, index, index + 1):
                for direction in (-1, 1):
                    perturbation_key = (neighboring, direction)
                    if perturbation_key not in perturbed_fits:
                        perturbed_fits[perturbation_key] = _fit(
                            quotes,
                            shift_index=neighboring,
                            shift=direction * _TICK,
                            deadline=deadline,
                        )
                    perturbations.append(perturbed_fits[perturbation_key])
            if any(candidate is None for candidate in perturbations) or any(
                abs(_digital(quotes, candidate, index) - probability) > _MAX_PERTURBATION
                for candidate in perturbations
                if candidate is not None
            ):
                odds[key] = OddsEstimate(None, "one_tick_unstable")
            else:
                odds[key] = OddsEstimate(probability, bounds=bounds)
    held_benchmark: dict[tuple[str, Decimal], bool] = {}
    quote_by_key = {
        (expiry, quote.strike): quote for expiry, quotes in by_expiry.items() for quote in quotes
    }
    for key in benchmark_keys:
        quote = quote_by_key.get(key)
        estimate = benchmark_odds.get(key) if benchmark_odds is not None else None
        price = estimate.held_out_vanilla_price if estimate is not None else None
        if quote is not None and price is not None and math.isfinite(price):
            held_benchmark[key] = quote.bid <= price <= quote.ask
    if benchmark_keys and not held_benchmark:
        rejected["benchmark_held_out_fit_unavailable"] += len(benchmark_keys)
    missing_cohort = len(benchmark_keys - held_quotes.keys())
    if missing_cohort:
        rejected["shadow_held_out_cohort_missing"] += missing_cohort
    for estimate in odds.values():
        if estimate.reason is not None:
            rejected[estimate.reason] += 1
    paired = held_shadow.keys() & held_benchmark.keys()
    return CurveShadowResult(
        odds,
        sum(held_shadow.values()),
        len(benchmark_keys) if benchmark_keys else len(held_quotes),
        (time.monotonic() - started) * 1000,
        dict(sorted(rejected.items())),
        shadow_held_out_predicted=len(held_shadow),
        benchmark_held_out_inside=sum(held_benchmark.values()),
        benchmark_held_out_predicted=len(held_benchmark),
        paired_held_out_count=len(paired),
        paired_shadow_inside=sum(held_shadow[key] for key in paired),
        paired_benchmark_inside=sum(held_benchmark[key] for key in paired),
        held_out_cohort=cohort,
        expiry_reasons=expiry_reasons,
        contract_reasons=contract_reasons,
    )
