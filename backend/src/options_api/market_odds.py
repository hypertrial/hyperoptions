"""Conservative risk-neutral expiry odds from a shared regime-switching option fit.

These are prices of European cash digitals, not forecasts of realized outcomes.
The public chain does not timestamp individual option quotes, so an estimate is
published only when the model, held-out quotes, and local vertical spreads agree.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from itertools import pairwise
from zoneinfo import ZoneInfo

import numpy as np
import regimelib as rl
from scipy.optimize import least_squares
from scipy.special import roots_legendre

from options_api.market_calendar import session_close, session_on_or_before
from options_api.models import OptionQuote

_NEW_YORK = ZoneInfo("America/New_York")
_YEAR_SECONDS = 365 * 24 * 60 * 60
_FIT_CUTOFF = 128.0
_CHECK_CUTOFFS = (128.0, 256.0, 512.0)
_MAX_FIT_SECONDS = 45.0
_MAX_PRICING_SECONDS = 60.0


@dataclass(frozen=True)
class OddsEstimate:
    call_itm_probability: float | None
    reason: str | None = None


@dataclass(frozen=True)
class _Quote:
    expiration: str
    strike: Decimal
    years: float
    rate: float
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class _CheckedNumericalEngine(rl.NumericalSwitchingEngine):
    """Pin the Fourier cutoff; use the numerical matrix solution for cash digitals.

    regimelib 0.1.0 chooses a cutoff from stationary-average volatility. In a
    slow, low-volatility starting regime that can omit a material tail (issue #2).
    Its digital path also calls an ODE for constant Black-Scholes forcing even
    though its vanilla path uses the exact matrix exponential. This narrow
    override shares that exact numerical solution for both payoff types.
    """

    def __init__(self, model: rl.SwitchingBlackScholesProcess, cutoff: float) -> None:
        super().__init__(model, regime=0, nodes=96)
        self.cutoff = cutoff
        self._characteristic_cache: dict[tuple[float, int, complex], np.ndarray] = {}

    def _frequencyLimit(self, maturity: float, k: float = 0.0) -> float:
        return self.cutoff

    def _a(self, g: object, gfuncs: object, maturity: float, a0: object = None) -> complex:
        return self._aVector(g, gfuncs, maturity, a0)[0][self.regime]

    def _characteristic(self, maturity: float, nodes: np.ndarray, shift: complex) -> np.ndarray:
        key = (maturity, len(nodes), shift)
        hit = self._characteristic_cache.get(key)
        if hit is None:
            values = []
            for frequency in nodes:
                g, gfuncs, prefactor = self.model.returnForcing(frequency + shift, maturity)
                values.append(prefactor() * self._aVector(g, gfuncs, maturity)[0][self.regime])
            hit = np.asarray(values)
            self._characteristic_cache[key] = hit
        return hit

    def _vanillaAll(self, option: rl.VanillaOption) -> dict[str, float]:
        maturity, strike = option.maturity, option.strike
        forward = self.model.forward(maturity)
        k = math.log(forward / strike)
        nodes, weights = _gauss_nodes(self.cutoff, self._nodeCount(self.cutoff, k))
        phi = self._characteristic(maturity, nodes, -0.5j)
        integral = np.dot(weights, (np.exp(1j * nodes * k) * phi).real / (nodes * nodes + 0.25))
        discount = math.exp(-self.model.r * maturity)
        call = discount * (forward - math.sqrt(forward * strike) * integral / math.pi)
        return {"value": call if option.isCall else call - discount * (forward - strike)}

    def _digital(self, option: rl.VanillaOption) -> dict[str, float]:
        maturity, strike = option.maturity, option.strike
        forward = self.model.forward(maturity)
        k = math.log(strike / forward)
        nodes, weights = _gauss_nodes(self.cutoff, self._nodeCount(self.cutoff, k))
        phi = self._characteristic(maturity, nodes, 0j)
        integral = np.dot(weights, (np.exp(-1j * nodes * k) * phi / (1j * nodes)).real)
        probability = 0.5 + integral / math.pi
        discount = math.exp(-self.model.r * maturity)
        return {"value": discount * (probability if option.isCall else 1 - probability)}


@lru_cache(maxsize=24)
def _gauss_nodes(cutoff: float, count: int) -> tuple[np.ndarray, np.ndarray]:
    nodes, weights = roots_legendre(count)
    return (nodes + 1) * cutoff / 2, weights * cutoff / 2


def _years_to_close(expiration: date, as_of: datetime) -> float:
    close = session_close(session_on_or_before(expiration))
    return (close - as_of).total_seconds() / _YEAR_SECONDS


def _price(
    model: rl.SwitchingBlackScholesProcess,
    engine: _CheckedNumericalEngine,
    quote: _Quote,
    *,
    digital: bool = False,
) -> float:
    payoff = (
        ("cash", "call", float(quote.strike), 1.0)
        if digital
        else ("call", float(quote.strike))
    )
    option = rl.VanillaOption(payoff, maturity=quote.years)
    option.setPricingEngine(engine)
    return float(option.NPV())


def _valid_quote(row: OptionQuote, years: float, rate: float, spot: float) -> _Quote | None:
    if row.identity_reason is not None or row.root != row.ticker:
        return None
    if row.call_bid is None or row.call_ask is None:
        return None
    bid, ask = float(row.call_bid), float(row.call_ask)
    strike = float(row.strike)
    if not all(map(math.isfinite, (bid, ask, strike))):
        return None
    if strike <= 0 or bid < 0 or ask <= bid or ask >= spot:
        return None
    if (row.call_open_interest or 0) < 5 and (row.call_volume or 0) < 5:
        return None
    # A crossed, zero-depth, or implausibly wide quote cannot constrain a tail.
    if ask - bid > max(0.25, 0.25 * (bid + ask) / 2):
        return None
    return _Quote(row.expiration, row.strike, years, rate, bid, ask)


def _sample_fit_quotes(
    by_expiry: dict[str, list[_Quote]], spot: float
) -> tuple[list[_Quote], list[_Quote]]:
    eligible: list[tuple[str, list[_Quote]]] = []
    for expiry, quotes in sorted(by_expiry.items()):
        near = [
            q for q in quotes
            if q.years >= 21 / 365 and 0.8 <= float(q.strike) / spot <= 1.2 and q.mid >= 0.2
        ]
        near.sort(key=lambda q: abs(math.log(float(q.strike) / spot)))
        if len(near) >= 3:
            eligible.append((expiry, near[:5]))
    if len(eligible) < 2:
        return [], []
    # Four distributed tenors bound work while covering the quoted term range.
    indexes = sorted({
        round(i * (len(eligible) - 1) / min(3, len(eligible) - 1))
        for i in range(min(4, len(eligible)))
    })
    train: list[_Quote] = []
    held_out: list[_Quote] = []
    for index in indexes:
        quotes = eligible[index][1]
        held_out.append(quotes[-1])
        train.extend(quotes[:-1])
    return train, held_out


def _model(spot: float, parameters: np.ndarray) -> rl.SwitchingBlackScholesProcess:
    low, high, from_first, from_second = parameters
    return rl.SwitchingBlackScholesProcess(
        rl.RegimeChain.twoState(float(from_first), float(from_second)),
        spot,
        0.0,
        0.0,
        [float(low), float(high)],
    )


def _with_rate(
    model: rl.SwitchingBlackScholesProcess, rate: float
) -> rl.SwitchingBlackScholesProcess:
    return rl.SwitchingBlackScholesProcess(model.chain, model.S0, rate, 0.0, model.sigma)


def _fit(
    train: list[_Quote], held_out: list[_Quote], spot: float
) -> rl.SwitchingBlackScholesProcess | None:
    if len(train) < 6 or len(held_out) < 2:
        return None
    started = time.monotonic()
    solutions: list[tuple[float, np.ndarray]] = []
    for initial in ((0.18, 0.38, 2.0, 2.0), (0.38, 0.18, 2.0, 2.0)):
        if time.monotonic() - started > _MAX_FIT_SECONDS:
            return None

        def residual(parameters: np.ndarray) -> np.ndarray:
            if time.monotonic() - started > _MAX_FIT_SECONDS:
                raise TimeoutError("market-odds calibration exceeded its time budget")
            try:
                base = _model(spot, parameters)
                priced: dict[
                    str, tuple[rl.SwitchingBlackScholesProcess, _CheckedNumericalEngine]
                ] = {}
                values = []
                for quote in train:
                    if quote.expiration not in priced:
                        fixed = _with_rate(base, quote.rate)
                        priced[quote.expiration] = (
                            fixed, _CheckedNumericalEngine(fixed, _FIT_CUTOFF)
                        )
                    fixed, engine = priced[quote.expiration]
                    values.append(
                        (_price(fixed, engine, quote) - quote.mid) / max(0.05, quote.spread)
                    )
            except (ArithmeticError, OverflowError, ValueError):
                return np.full(len(train), 1e6)
            if not all(map(math.isfinite, values)):
                return np.full(len(train), 1e6)
            return np.asarray(values)

        try:
            result = least_squares(
                residual,
                initial,
                bounds=((0.07, 0.07, 0.05, 0.05), (1.5, 1.5, 12.0, 12.0)),
                max_nfev=24,
                ftol=1e-4,
                xtol=1e-4,
                gtol=1e-4,
            )
        except (ArithmeticError, OverflowError, TimeoutError, ValueError):
            return None
        if np.all(np.isfinite(result.x)) and math.isfinite(result.cost):
            solutions.append((float(result.cost), result.x))
    if not solutions:
        return None
    solutions.sort(key=lambda item: item[0])
    best_cost, best_parameters = solutions[0]
    if math.sqrt(2 * best_cost / len(train)) > 1.25:
        return None
    model = _model(spot, best_parameters)
    # Only prices actually inside (or within one tick of) held-out markets pass.
    try:
        for quote in [*train, *held_out]:
            price = _price_converged(model, quote, digital=False)
            if price is None:
                return None
            tolerance = max(0.05, 0.0005 * spot)
            if not quote.bid - tolerance <= price <= quote.ask + tolerance:
                return None
        if len(solutions) > 1 and solutions[1][0] <= best_cost * 1.15 + 0.1:
            alternative = _model(spot, solutions[1][1])
            for quote in held_out:
                growth = math.exp(quote.rate * quote.years)
                first = _with_rate(model, quote.rate)
                second = _with_rate(alternative, quote.rate)
                left = _price(
                    first, _CheckedNumericalEngine(first, _CHECK_CUTOFFS[-1]), quote, digital=True
                ) * growth
                right = _price(
                    second, _CheckedNumericalEngine(second, _CHECK_CUTOFFS[-1]), quote, digital=True
                ) * growth
                if abs(left - right) > 0.03:
                    return None
    except (ArithmeticError, OverflowError, ValueError):
        return None
    return model


def _price_converged(
    model: rl.SwitchingBlackScholesProcess,
    quote: _Quote,
    *,
    digital: bool,
    engines: dict[
        tuple[str, float], tuple[rl.SwitchingBlackScholesProcess, _CheckedNumericalEngine]
    ] | None = None,
) -> float | None:
    # For switching Black-Scholes, min(sigma) bounds Fourier-tail decay on
    # every regime path. This is the safeguard absent from regimelib issue #2.
    if min(model.sigma) * math.sqrt(quote.years) * _CHECK_CUTOFFS[-1] < 7.0:
        return None
    values: list[float] = []
    try:
        for cutoff in _CHECK_CUTOFFS:
            key = quote.expiration, cutoff
            cached = engines.get(key) if engines is not None else None
            if cached is None:
                fixed = _with_rate(model, quote.rate)
                cached = fixed, _CheckedNumericalEngine(fixed, cutoff)
                if engines is not None:
                    engines[key] = cached
            value = _price(cached[0], cached[1], quote, digital=digital)
            if not math.isfinite(value):
                return None
            values.append(value)
    except (ArithmeticError, OverflowError, ValueError):
        return None
    tolerance = (0.002 if digital else max(0.01, 0.0002 * model.S0))
    if abs(values[1] - values[2]) > tolerance or abs(values[0] - values[1]) > 2 * tolerance:
        return None
    return values[-1]


def _vertical_bounds_with_reason(
    quotes: list[_Quote], target: _Quote
) -> tuple[tuple[float, float] | None, str | None]:
    # Every clean call vertical gives a model-free bound. Distant strikes can
    # suppress bid/ask noise, while nearby strikes limit payoff curvature;
    # intersecting all of them is at least as tight as adjacent-only bounds.
    growth = math.exp(target.rate * target.years)
    lower, upper = 0.0, 1.0
    has_left = has_right = False
    for quote in quotes:
        if quote.strike < target.strike:
            has_left = True
            distance = float(target.strike - quote.strike)
            upper = min(upper, (quote.ask - target.bid) / distance * growth)
        elif quote.strike > target.strike:
            has_right = True
            distance = float(quote.strike - target.strike)
            lower = max(lower, (target.bid - quote.ask) / distance * growth)
    if not has_left or not has_right:
        return None, "quote_bracket_missing"
    if lower > upper:
        return None, "quote_bounds_inconsistent"
    if upper - lower > 0.10:
        return None, "quote_bounds_wide"
    return (lower, upper), None


def _vertical_bounds(quotes: list[_Quote], target: _Quote) -> tuple[float, float] | None:
    return _vertical_bounds_with_reason(quotes, target)[0]


def calculate_market_odds(
    rows: Sequence[OptionQuote],
    spot: Decimal,
    rate_for_expiry: Callable[[date], float | None],
    allowed_expirations: set[str],
    as_of: datetime,
) -> dict[tuple[str, Decimal], OddsEstimate]:
    """Fit once per ticker snapshot and return conservative call ITM odds by contract.

    Put ITM is one minus the returned call ITM probability. A missing rate,
    nonstandard contract, bad fit, or unverifiable near-expiry quote yields a
    reason instead of a number.
    """
    result = {
        (row.expiration, row.strike): OddsEstimate(None, "insufficient_quotes")
        for row in rows
        if row.expiration in allowed_expirations
    }
    if not result:
        return result
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=ZoneInfo("UTC"))
    try:
        price = float(spot)
    except (TypeError, ValueError, OverflowError):
        price = math.nan
    if not math.isfinite(price) or price <= 0:
        return {key: OddsEstimate(None, "invalid_spot") for key in result}

    rows_by_key: dict[tuple[str, Decimal], list[OptionQuote]] = defaultdict(list)
    by_expiry: dict[str, list[_Quote]] = defaultdict(list)
    # All rows at an expiry share one exchange close and one dated curve rate.
    context: dict[str, tuple[float, float] | str] = {}
    local_day = as_of.astimezone(_NEW_YORK).date()
    for expiration in {key[0] for key in result}:
        try:
            expiry = date.fromisoformat(expiration)
            expiry_session = session_on_or_before(expiry)
        except (KeyError, ValueError):
            context[expiration] = "invalid_expiration"
            continue
        if expiry_session < local_day:
            context[expiration] = "expired"
            continue
        if expiry_session == local_day:
            context[expiration] = "same_day_quote_timing"
            continue
        years = _years_to_close(expiry, as_of)
        try:
            rate = rate_for_expiry(expiry)
        except (ArithmeticError, TypeError, ValueError):
            rate = None
        if rate is None or not math.isfinite(rate) or not 0 <= rate <= 0.25:
            context[expiration] = "rate_unavailable"
        else:
            context[expiration] = years, float(rate)
    for row in rows:
        key = (row.expiration, row.strike)
        if key not in result:
            continue
        rows_by_key[key].append(row)
        if row.identity_reason is not None or row.root != row.ticker:
            result[key] = OddsEstimate(None, "invalid_contract")
            continue
        term = context[row.expiration]
        if isinstance(term, str):
            result[key] = OddsEstimate(None, term)
            continue
        years, rate = term
        quote = _valid_quote(row, years, rate, price)
        if quote is None:
            result[key] = OddsEstimate(None, "invalid_quote")
            continue
        by_expiry[row.expiration].append(quote)

    # A duplicate standard contract is ambiguous even if one quote is usable.
    for key, matches in rows_by_key.items():
        if len(matches) != 1:
            result[key] = OddsEstimate(None, "duplicate_contract")
            by_expiry[key[0]] = [q for q in by_expiry[key[0]] if q.strike != key[1]]
    for quotes in by_expiry.values():
        quotes.sort(key=lambda q: q.strike)

    train, held_out = _sample_fit_quotes(by_expiry, price)
    model = _fit(train, held_out, price)
    if model is None:
        valid_keys = {(q.expiration, q.strike) for qs in by_expiry.values() for q in qs}
        return {
            key: OddsEstimate(None, "calibration_failed")
            if estimate.reason == "insufficient_quotes" or key in valid_keys
            else estimate
            for key, estimate in result.items()
        }

    pricing_started = time.monotonic()
    priced_engines: dict[
        tuple[str, float], tuple[rl.SwitchingBlackScholesProcess, _CheckedNumericalEngine]
    ] = {}
    for expiry, quotes in by_expiry.items():
        for quote in quotes:
            key = (expiry, quote.strike)
            bounds, bounds_reason = _vertical_bounds_with_reason(quotes, quote)
            if bounds is None:
                result[key] = OddsEstimate(None, bounds_reason)
                continue
            if time.monotonic() - pricing_started > _MAX_PRICING_SECONDS:
                result[key] = OddsEstimate(None, "pricing_budget_exceeded")
                continue
            vanilla = _price_converged(model, quote, digital=False, engines=priced_engines)
            digital = _price_converged(model, quote, digital=True, engines=priced_engines)
            if vanilla is None or digital is None:
                result[key] = OddsEstimate(None, "numerical_unstable")
                continue
            if not quote.bid - 0.05 <= vanilla <= quote.ask + 0.05:
                result[key] = OddsEstimate(None, "quote_fit_failed")
                continue
            probability = digital * math.exp(quote.rate * quote.years)
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                result[key] = OddsEstimate(None, "numerical_unstable")
            elif not bounds[0] - 0.015 <= probability <= bounds[1] + 0.015:
                result[key] = OddsEstimate(None, "quote_bounds_mismatch")
            else:
                result[key] = OddsEstimate(probability)

        # A numerical or quote contradiction in one expiry invalidates its
        # published sequence, since a single risk-neutral CDF must be monotone.
        published = [(q.strike, result[(expiry, q.strike)].call_itm_probability) for q in quotes]
        published = [(strike, p) for strike, p in published if p is not None]
        if any(left < right - 0.001 for (_, left), (_, right) in pairwise(published)):
            for strike, _ in published:
                result[(expiry, strike)] = OddsEstimate(None, "nonmonotone_odds")
    return result
