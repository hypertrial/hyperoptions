"""Independent scalar oracles and causal/offline historical calibration contracts."""

from dataclasses import replace
from datetime import date
from math import isclose

import numpy as np
import polars as pl
import pytest

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.physical_contest import PhysicalShadowForecaster, ShadowForecast
from stocksweeper.forecast.predictive import PredictiveDistribution, PredictiveForecaster
from stocksweeper.research.historical_options.calibration import (
    MODELS,
    FrozenPrices,
    panel_for,
    run_calibration,
)
from stocksweeper.research.historical_options.payoff import (
    WeightedPayoffIntegrator,
    pinball_loss,
)


def _distribution() -> PredictiveDistribution:
    return PredictiveDistribution(
        "CIFR", "available", None, "empirical_scaled", date(2024, 11, 25),
        date(2024, 11, 27), 2, spot=100, daily_volatility=0.02,
        model_version="test-v1", terminal_prices=(50, 90, 100, 110, 160),
        weights=(0.02, 0.03, 0.15, 0.4, 0.4), support=5,
    )


@pytest.mark.parametrize("side", ["call", "put"])
def test_vector_integrator_matches_independent_scalar_and_app_metrics(side):
    distribution = _distribution()
    integrator = WeightedPayoffIntegrator(distribution.prices, distribution.weights)
    strikes = np.asarray([70.125, 90, 100, 200])
    premiums = np.asarray([0.125, 2.5, 7.025, 5])
    vector = integrator.integrate(side, strikes, premiums, 100)
    for index, (strike, premium) in enumerate(zip(strikes, premiums, strict=True)):
        outcomes = [
            min(value, strike) - 100 + premium if side == "call"
            else premium - max(strike - value, 0)
            for value in distribution.prices
        ]
        expected = sum(
            weight * pnl for pnl, weight in zip(outcomes, distribution.weights, strict=True)
        )
        loss = sum(
            weight for pnl, weight in zip(outcomes, distribution.weights, strict=True) if pnl < 0
        )
        cumulative = 0
        fifth = None
        for pnl, weight in sorted(zip(outcomes, distribution.weights, strict=True)):
            cumulative += weight
            if cumulative >= 0.05:
                fifth = pnl
                break
        assert vector.expected_pnl[index] == pytest.approx(expected)
        assert vector.loss_probability[index] == pytest.approx(loss)
        assert vector.quantile05[index] == pytest.approx(fifth)
        app = distribution.payoff_metrics(side, strike, premium)
        assert vector.expected_pnl[index] == pytest.approx(app.expected_pnl)
        assert vector.loss_probability[index] == pytest.approx(app.loss_probability)
        assert vector.quantile05[index] == pytest.approx(app.fifth_percentile_pnl)


def test_anchor_entry_cost_strict_breakeven_and_negative_premium_are_independent():
    integrator = WeightedPayoffIntegrator([90, 100, 110], [0.1, 0.4, 0.5])
    call = integrator.integrate("call", [105, 95], [0, -1], [100, 100])
    assert call.loss_probability.tolist() == pytest.approx([0.1, 1])
    assert call.expected_pnl.tolist() == pytest.approx([1.5, -6.5])
    put = integrator.integrate("put", [100, 100], [0, -0.0065], [999, 999])
    assert put.loss_probability.tolist() == pytest.approx([0.1, 1])
    assert put.expected_pnl.tolist() == pytest.approx([-1, -1.0065])


def test_weighted_left_quantiles_atoms_and_strict_itm():
    integrator = WeightedPayoffIntegrator([110, 90, 100, 90], [0.4, 0.02, 0.55, 0.03])
    assert integrator.weighted_left_quantile(0.05) == 90
    assert integrator.weighted_left_quantile(0.05001) == 100
    assert integrator.strict_itm("call", [90, 100]).tolist() == pytest.approx([0.95, 0.4])
    assert integrator.strict_itm("put", [90, 100]).tolist() == pytest.approx([0, 0.05])
    call = integrator.integrate("call", [80, 95], [20, 5], 100)
    assert call.quantile05.tolist() == pytest.approx([0, -5])
    assert call.cap_atom.tolist() == pytest.approx([1, 0.95])
    assert call.quantile_atom.tolist() == pytest.approx([1, 0.05])
    assert pinball_loss(0, -5) == pytest.approx(0.25)
    assert pinball_loss(-10, -5) == pytest.approx(4.75)


def test_vector_crps_matches_independent_quadratic_oracle_without_clipping():
    prices, weights = [1, 100, 1e200], [0.1, 0.8, 0.1]
    integrator = WeightedPayoffIntegrator(prices, weights)
    first = sum(w * abs(value - 105) for value, w in zip(prices, weights, strict=True))
    second = sum(
        wi * wj * abs(a - b)
        for a, wi in zip(prices, weights, strict=True)
        for b, wj in zip(prices, weights, strict=True)
    ) / 2
    assert integrator.crps(105) == pytest.approx(first - second)
    assert integrator.crps(105) > 1e190


@pytest.mark.parametrize("prices,weights", [
    ([], []), ([1, float("inf")], [0.5, 0.5]), ([1, 2], [0.5, -0.5]),
    ([1, 2], [0.4, 0.4]), ([0, 1], [0.5, 0.5]), ([1, 2], [1]),
])
def test_invalid_scenarios_are_rejected(prices, weights):
    with pytest.raises(ValueError):
        WeightedPayoffIntegrator(prices, weights)


def _prices(start=date(2024, 1, 2), end=date(2024, 11, 27)) -> pl.DataFrame:
    sessions = SessionCalendar().sessions(start, end)
    prices = 100 * np.exp(np.random.default_rng(15).normal(0, 0.01, len(sessions)).cumsum())
    return pl.DataFrame({
        "ticker": ["CIFR"] * len(sessions), "ts": sessions,
        "open": prices + 2, "close": prices, "high": prices + 3, "low": prices - 1,
        "volume": [1000.0] * len(sessions), "dividends": [0.0] * len(sessions),
        "stock_splits": [0.0] * len(sessions),
    })


def _days() -> pl.DataFrame:
    return pl.DataFrame({
        "ticker": ["CIFR"] * 3, "contract": ["C1", "P1", "C2"],
        "session": [date(2024, 11, 25)] * 3,
        "expiry": [date(2024, 11, 27)] * 3,
        "expiry_session": [date(2024, 11, 27)] * 3,
        "side": ["call", "put", "call"], "strike": [100.0, 100.0, 90.0],
        "mark": [2.0, 2.0, 5.0], "volume": [30.0, 40.0, 5.0],
        "transactions": [5.0, 6.0, 1.0], "valid": [True] * 3,
        "reason": [None] * 3, "reconciliation_failed": [False, False, True],
    })


def test_calibration_uses_same_origin_mark_for_predicted_and_realized_pnl(
    tmp_path, monkeypatch
):
    calls = []
    distribution = _distribution()

    def candidates(_self, ticker, as_of, expiry):
        calls.append((ticker, as_of, expiry))
        return {model: ShadowForecast(replace(distribution, method=model), None, 0, 0)
                for model in MODELS}

    monkeypatch.setattr(PhysicalShadowForecaster, "forecast_candidates", candidates)
    prices = _prices()
    summary = run_calibration(_days(), prices, tmp_path)
    cells = pl.read_parquet(tmp_path / "forecast_cells.parquet")
    assert cells.height == 3 * 8
    assert len(calls) == 1
    assert calls[0][1].hour == 21 and calls[0][1].second == 1
    close = prices.filter(pl.col("ts") == date(2024, 11, 25))["close"][0]
    terminal = prices.filter(pl.col("ts") == date(2024, 11, 27))["close"][0]
    row = cells.filter((pl.col("model") == "empirical_scaled") & (pl.col("contract") == "C1"))
    assert row["realized_pnl"][0] == pytest.approx(min(terminal, 100) - close + 2)
    assert row["decision_close"][0] == close
    assert row["forecast_anchor"][0] == 100  # independent distribution anchor is preserved
    assert cells.filter(pl.col("contract") == "C2")["primary_eligible"].to_list() == [False] * 8
    assert cells.filter(pl.col("contract") == "C2")["brier"].null_count() == 0
    assert summary["counts"]["exact_horizon_contract_days"] == 3


def test_future_price_perturbation_does_not_change_origin_forecast(tmp_path, monkeypatch):
    from stocksweeper.forecast import physical_contest

    # Keep this a native causal forecast test, with optimization removed for speed.
    for name in ("_fit_student", "_fit_gjr", "_fit_har", "_fit_arch_skew", "_fit_markov"):
        monkeypatch.setattr(physical_contest, name, lambda *_args, **_kwargs: (None, "fixture"))
    frames = _prices()
    changed = frames.with_columns(
        pl.when(pl.col("ts") > date(2024, 11, 25)).then(pl.col("close") * 0.1)
        .otherwise(pl.col("close")).alias("close")
    )
    results = []
    for index, stocks in enumerate([frames, changed]):
        run_calibration(_days(), stocks, tmp_path / str(index))
        results.append(pl.read_parquet(tmp_path / str(index) / "forecast_cells.parquet"))
    first = results[0].filter(pl.col("model") == "lognormal_ewma")
    second = results[1].filter(pl.col("model") == "lognormal_ewma")
    for field in (
        "prediction", "expected_pnl", "loss_probability", "quantile05", "forecast_anchor"
    ):
        assert first[field].to_list() == second[field].to_list()
    assert first["realized_pnl"].to_list() != second["realized_pnl"].to_list()


@pytest.mark.parametrize("invalid", [False, True])
def test_offline_slow_fit_bypass_preserves_live_default_and_numerical_failure(
    tmp_path, monkeypatch, invalid
):
    from stocksweeper.forecast import physical_contest

    frame = _prices().drop("ticker")
    session = frame["ts"][-1]
    clock = [0.0]
    monkeypatch.setattr(physical_contest, "perf_counter", lambda: clock[0])

    def student(*_args):
        clock[0] += 5.001
        return (None, "student_parameters_invalid") if invalid else ((5.0, 0.8), None)

    monkeypatch.setattr(physical_contest, "_fit_student", student)
    for name in ("_fit_gjr", "_fit_har", "_fit_arch_skew", "_fit_markov"):
        monkeypatch.setattr(physical_contest, name, lambda *_args, **_kwargs: (None, "fixture"))
    live = PhysicalShadowForecaster(PredictiveForecaster(tmp_path))
    offline = PhysicalShadowForecaster(PredictiveForecaster(tmp_path), enforce_fit_latency=False)
    live_fit = live._fit("CIFR", session, frame, "live")[0]
    offline_fit = offline._fit("CIFR", session, frame, "offline")[0]
    assert live_fit.t_parameters is None
    assert live_fit.t_reason == "student_fit_latency_exceeded"
    assert offline_fit.t_parameters == (None if invalid else (5.0, 0.8))
    assert offline_fit.t_reason == ("student_parameters_invalid" if invalid else None)
    assert isclose(offline_fit.t_fit_ms, 5001)


def test_identity_boundaries_panel_and_read_only_adapter():
    assert panel_for("NBIS", date(2026, 1, 2), date(2026, 1, 9)) == "other_eligible"
    assert panel_for("CIFR", date(2025, 12, 31), date(2026, 1, 2)) == "other_eligible"
    assert panel_for("IREN", date(2026, 1, 2), date(2026, 1, 9)) == "supplement"
    store = FrozenPrices({"CIFR": _prices()})
    assert store.read("NBIS") is None
    assert not hasattr(store, "prepare") and not hasattr(store, "update")


def test_non_session_audit_day_does_not_abort_calibration(tmp_path, monkeypatch):
    distribution = _distribution()
    monkeypatch.setattr(PhysicalShadowForecaster, "forecast_candidates",
                        lambda *_args: {model: ShadowForecast(
                            replace(distribution, method=model), None, 0, 0) for model in MODELS})
    invalid = _days().head(1).with_columns(
        pl.lit(date(2024, 11, 23)).alias("session"),
        pl.lit(False).alias("valid"), pl.lit("no_regular_session_bar").alias("reason"))
    summary = run_calibration(
        pl.concat([_days(), invalid], how="vertical_relaxed"), _prices(), tmp_path
    )
    assert summary["counts"]["invalid_non_session_contract_days"] == 1
    assert pl.read_parquet(tmp_path / "forecast_cells.parquet").height == 24
