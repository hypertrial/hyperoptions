"""Research-only integration of monotone short-option payoffs.

The forecast anchor belongs to the distribution. Entry costs are independent
arguments; no quote-source type or forecast reanchoring is involved. Prices and
payoffs are per share, before costs, unless the caller scales them.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def pinball_loss(observed: float, forecast_quantile: float, q: float = 0.05) -> float:
    if not 0 < q < 1:
        raise ValueError("quantile probability must be strictly between zero and one")
    residual = observed - forecast_quantile
    return float(max(q * residual, (q - 1) * residual))


@dataclass(frozen=True)
class PayoffIntegrals:
    expected_pnl: np.ndarray
    loss_probability: np.ndarray
    quantile05: np.ndarray
    cap_atom: np.ndarray
    quantile_atom: np.ndarray


class WeightedPayoffIntegrator:
    """O(n log n) setup and O(log n) per strike, without a strike×scenario matrix."""

    def __init__(self, prices, weights) -> None:
        values = np.asarray(prices, dtype=float)
        masses = np.asarray(weights, dtype=float)
        if (
            values.ndim != 1
            or masses.shape != values.shape
            or not len(values)
            or not np.isfinite(values).all()
            or np.any(values <= 0)
            or not np.isfinite(masses).all()
            or np.any(masses < 0)
            or not np.isclose(masses.sum(), 1, rtol=0, atol=1e-8)
        ):
            raise ValueError("finite positive prices and unit-sum nonnegative weights required")
        ordering = np.argsort(values, kind="stable")
        self.prices = values[ordering]
        self.weights = masses[ordering]
        # Normalize only the documented sum tolerance, never alter terminal prices.
        self.weights = self.weights / self.weights.sum()
        self.cumulative = np.concatenate(([0.0], np.cumsum(self.weights)))
        self.cumulative[-1] = 1.0
        with np.errstate(over="ignore", invalid="ignore"):
            self.moments = np.concatenate(([0.0], np.cumsum(self.weights * self.prices)))
        self.q05_price = self.weighted_left_quantile(0.05)

    def weighted_left_quantile(self, q: float) -> float:
        if not 0 < q <= 1:
            raise ValueError("quantile probability must be in (0, 1]")
        index = int(np.searchsorted(self.cumulative[1:], q, side="left"))
        return float(self.prices[min(index, len(self.prices) - 1)])

    def cdf(self, prices, *, inclusive: bool = False) -> np.ndarray:
        indexes = np.searchsorted(self.prices, prices, side="right" if inclusive else "left")
        return self.cumulative[indexes]

    def strict_itm(self, side: str, strikes) -> np.ndarray:
        if side == "call":
            return 1 - self.cdf(strikes, inclusive=True)
        if side == "put":
            return self.cdf(strikes)
        raise ValueError("side must be call or put")

    def integrate(self, side: str, strikes, premiums, entry_costs) -> PayoffIntegrals:
        strikes, premiums, entry_costs = np.broadcast_arrays(
            np.asarray(strikes, dtype=float),
            np.asarray(premiums, dtype=float),
            np.asarray(entry_costs, dtype=float),
        )
        if (
            not np.isfinite(strikes).all()
            or np.any(strikes <= 0)
            or not np.isfinite(premiums).all()
            or not np.isfinite(entry_costs).all()
        ):
            raise ValueError("finite inputs and positive strikes required")
        indexes = np.searchsorted(self.prices, strikes, side="left")
        below = self.cumulative[indexes]
        moments = self.moments[indexes]
        cap_atom = 1 - below
        with np.errstate(over="ignore", invalid="ignore"):
            if side == "call":
                expected = moments + strikes * cap_atom - entry_costs + premiums
                cap = strikes - entry_costs + premiums
                quantile = np.minimum(self.q05_price, strikes) - entry_costs + premiums
                loss = np.where(cap < 0, 1.0, self.cdf(entry_costs - premiums))
            elif side == "put":
                expected = premiums - (strikes * below - moments)
                cap = premiums
                quantile = premiums - np.maximum(strikes - self.q05_price, 0)
                loss = np.where(cap < 0, 1.0, self.cdf(strikes - premiums))
            else:
                raise ValueError("side must be call or put")
        price_atom = float(
            self.cdf(self.q05_price, inclusive=True) - self.cdf(self.q05_price)
        )
        quantile_atom = np.where(self.q05_price >= strikes, cap_atom, price_atom)
        return PayoffIntegrals(expected, loss, quantile, cap_atom, quantile_atom)

    def crps(self, observed: float) -> float:
        if not np.isfinite(observed) or observed <= 0:
            raise ValueError("finite positive observed price required")
        with np.errstate(over="ignore", invalid="ignore"):
            first = np.sum(self.weights * np.abs(self.prices - observed))
            second = np.sum(
                self.weights
                * (self.prices * self.cumulative[:-1] - self.moments[:-1])
            )
            return float(first - second)
