"""Forecast availability, provenance, strict tails, and bounded selection."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from math import exp, isclose

import polars as pl
import pytest

from options_api.market_calendar import session_close
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import ForecastPriceStore
from stocksweeper.forecast.predictive import (
    PredictiveDistribution,
    PredictiveForecaster,
    _empirical_evidence,
    _independent_samples,
    _prior_samples,
    evaluate_predictive_history,
)

COMPLETED = date(2026, 9, 24)
AS_OF = datetime(2026, 9, 24, 23, tzinfo=UTC)
EXPIRY = date(2026, 10, 1)


def _bars(count: int = 100, *, last: date = COMPLETED) -> pl.DataFrame:
    sessions = SessionCalendar().sessions(date(2020, 1, 1), last)[-count:]
    price = 100.0
    closes = []
    for i in range(len(sessions)):
        price *= exp(0.006 if i % 3 else -0.004)
        closes.append(price)
    return pl.DataFrame(
        {
            "ts": sessions,
            "open": closes,
            "high": [value * 1.01 for value in closes],
            "low": [value * 0.99 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(sessions),
            "dividends": [0.0] * len(sessions),
            "stock_splits": [0.0] * len(sessions),
        }
    )


class Prices:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.calls = 0

    def fetch(self, ticker, start, end):
        self.calls += 1
        frame = self.frame.filter(pl.col("ts") <= end)
        return frame if start is None else frame.filter(pl.col("ts") >= start)


class NoNetwork:
    def fetch(self, ticker, start, end):
        raise AssertionError("forecast() must not fetch prices")


def test_baseline_uses_exact_completed_close_and_local_cache_after_restart(tmp_path):
    provider = Prices(_bars())
    worker = PredictiveForecaster(tmp_path, provider)
    worker.prepare("AAPL", COMPLETED)
    assert provider.calls == 1
    result = PredictiveForecaster(tmp_path, NoNetwork()).forecast("AAPL", AS_OF, EXPIRY)
    assert result.status == "available"
    assert result.method == "lognormal_ewma"
    assert result.as_of == COMPLETED
    assert result.spot == _bars()["close"][-1]
    assert result.data_hash is not None
    assert len(result.prices) == len(result.weights) == 1024
    assert isclose(sum(result.weights), 1.0)
    assert result.probability("call", result.spot) == pytest.approx(0.5)
    assert result.probability("put", result.spot) == pytest.approx(0.5)


def test_missing_latest_close_and_lookback_fail_closed(tmp_path):
    provider = Prices(_bars(last=date(2026, 9, 23)))
    forecaster = PredictiveForecaster(tmp_path, provider)
    with pytest.raises(ValueError, match="market_data_missing"):
        forecaster.prepare("AAPL", COMPLETED)
    assert forecaster.forecast("AAPL", AS_OF, EXPIRY).reason == "market_data_missing"

    short = PredictiveForecaster(tmp_path / "short", Prices(_bars(count=60)))
    short.prepare("AAPL", COMPLETED)
    assert short.forecast("AAPL", AS_OF, EXPIRY).reason == "ticker_history_short"


def test_just_after_exchange_close_requires_that_sessions_bar(tmp_path):
    friday = date(2026, 9, 25)
    as_of = session_close(friday) + timedelta(seconds=1)
    provider = Prices(_bars(last=COMPLETED))
    forecaster = PredictiveForecaster(tmp_path, provider)
    forecaster.prepare("AAPL", COMPLETED)

    pending = forecaster.forecast("AAPL", as_of, EXPIRY)
    assert pending.status == "unavailable"
    assert pending.reason == "market_data_missing"
    assert pending.as_of == friday

    provider.frame = _bars(count=101, last=friday)
    forecaster.prepare("AAPL", friday)
    ready = forecaster.forecast("AAPL", as_of, EXPIRY)
    assert ready.status == "available"
    assert ready.as_of == friday
    assert ready.spot == provider.frame["close"][-1]


def test_verified_cache_rejects_manifest_hash_schema_and_session_mismatch(tmp_path):
    store = ForecastPriceStore(tmp_path, Prices(_bars()))
    store.update("AAPL", COMPLETED)
    path = store.path("AAPL")
    manifest = path.with_suffix(".json")
    original = json.loads(manifest.read_text())
    for changed in (
        {**original, "hash": "0" * 64},
        {**original, "through_session": "2026-09-23"},
    ):
        manifest.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="invalid forecast price cache"):
            store.read("AAPL")
    manifest.write_text(json.dumps(original))
    _bars().with_columns(pl.col("close").cast(pl.Float32)).write_parquet(path)
    with pytest.raises(ValueError, match="invalid forecast price cache"):
        store.read("AAPL")


def test_split_since_contract_creation_and_invalid_terms_withhold_odds(tmp_path):
    bars = _bars().with_columns(
        pl.when(pl.col("ts") == date(2026, 9, 22))
        .then(2.0)
        .otherwise(pl.col("stock_splits"))
        .alias("stock_splits")
    )
    forecaster = PredictiveForecaster(tmp_path, Prices(bars))
    forecaster.prepare("AAPL", COMPLETED)
    assert forecaster.forecast(
        "AAPL", AS_OF, EXPIRY, contract_since=date(2026, 9, 21)
    ).reason == "contract_terms_changed"
    assert forecaster.forecast(
        "AAPL", AS_OF, EXPIRY, contract_since=date(2026, 9, 23)
    ).status == "available"
    assert forecaster.forecast("AAPL", AS_OF, EXPIRY, standard_terms=False).reason == (
        "contract_terms_ambiguous"
    )

    invalid = _bars().with_columns(
        pl.when(pl.col("ts") == date(2026, 9, 22))
        .then(float("nan"))
        .otherwise(pl.col("stock_splits"))
        .alias("stock_splits")
    )
    ambiguous = PredictiveForecaster(tmp_path / "invalid", Prices(invalid))
    ambiguous.prepare("AAPL", COMPLETED)
    assert ambiguous.forecast(
        "AAPL", AS_OF, EXPIRY, contract_since=date(2026, 9, 21)
    ).reason == "contract_terms_ambiguous"


def test_missing_history_bar_and_manifest_corruption_are_unavailable(tmp_path):
    bars = _bars().filter(pl.col("ts") != date(2026, 9, 16))
    forecaster = PredictiveForecaster(tmp_path, Prices(bars))
    forecaster.prepare("AAPL", COMPLETED)
    assert forecaster.forecast("AAPL", AS_OF, EXPIRY).reason == "ticker_history_short"

    store = ForecastPriceStore(tmp_path / "broken", Prices(_bars()))
    store.update("AAPL", COMPLETED)
    store.path("AAPL").with_suffix(".json").write_text("broken")
    result = PredictiveForecaster(tmp_path / "broken", NoNetwork()).forecast(
        "AAPL", AS_OF, EXPIRY
    )
    assert result.reason == "market_data_invalid"

    early_gap = _bars().filter(pl.col("ts") != _bars()["ts"][5])
    watched = PredictiveForecaster(tmp_path / "watched", Prices(early_gap))
    watched.prepare("AAPL", COMPLETED)
    assert watched.forecast(
        "AAPL", AS_OF, EXPIRY, contract_since=_bars()["ts"][0]
    ).reason == "contract_terms_ambiguous"


def test_full_refresh_repairs_corrupt_cache_only_with_valid_new_prices(tmp_path):
    provider = Prices(_bars())
    forecaster = PredictiveForecaster(tmp_path, provider)
    forecaster.prepare("AAPL", COMPLETED)
    forecaster.prices.path("AAPL").with_suffix(".json").write_text("broken")
    restarted = PredictiveForecaster(tmp_path, provider)
    assert restarted.forecast("AAPL", AS_OF, EXPIRY).reason == "market_data_invalid"
    restarted.prepare("AAPL", COMPLETED)
    assert provider.calls == 2
    assert restarted.forecast("AAPL", AS_OF, EXPIRY).status == "available"


def test_weekend_holiday_early_close_and_one_year_limit(tmp_path):
    friday = date(2026, 11, 27)  # Early close after Thanksgiving.
    forecaster = PredictiveForecaster(tmp_path, Prices(_bars(last=friday)))
    forecaster.prepare("AAPL", friday)
    after_early_close = datetime(2026, 11, 27, 18, 45, tzinfo=UTC)
    friday_result = forecaster.forecast("AAPL", after_early_close, date(2026, 12, 4))
    assert friday_result.as_of == friday
    assert friday_result.status == "available"
    weekend = forecaster.forecast(
        "AAPL", datetime(2026, 11, 28, 18, tzinfo=UTC), date(2026, 12, 4),
        contract_since=date(2026, 11, 28),
    )
    assert weekend.as_of == friday
    assert weekend.status == "available"
    one_year = forecaster.calendar.expiry_session(date(2027, 11, 27))
    assert forecaster.forecast("AAPL", after_early_close, one_year).status == "available"
    too_long = forecaster.calendar.offset(one_year, 1)
    assert forecaster.forecast("AAPL", after_early_close, too_long).reason == (
        "horizon_unsupported"
    )


@pytest.mark.parametrize(
    ("completed", "horizon", "expiry", "expected_status"),
    [
        (date(2026, 1, 2), 252, date(2027, 1, 5), "unavailable"),
        (date(2027, 2, 16), 253, date(2028, 2, 16), "available"),
    ],
)
def test_one_calendar_year_limit_is_not_a_fixed_session_count(
    tmp_path, completed: date, horizon: int, expiry: date, expected_status: str
) -> None:
    forecaster = PredictiveForecaster(tmp_path, Prices(_bars(last=completed)))
    forecaster.prepare("AAPL", completed)
    assert forecaster.calendar.offset(completed, horizon) == expiry
    result = forecaster.forecast(
        "AAPL", session_close(completed) + timedelta(seconds=1),
        expiry,
    )
    assert result.horizon_sessions == horizon
    assert result.status == expected_status
    assert result.reason == ("horizon_unsupported" if horizon == 252 else None)


def test_discrete_strict_atm_and_hypothetical_payoff_metrics():
    result = PredictiveDistribution(
        ticker="AAPL", status="available", reason=None, method="empirical_scaled",
        as_of=COMPLETED, expiry_session=EXPIRY, horizon_sessions=5,
        spot=100.0, daily_volatility=0.02, model_version="test", support=3,
        terminal_prices=(90.0, 100.0, 110.0), weights=(1 / 3,) * 3,
    )
    assert result.probability("call", 100) == pytest.approx(1 / 3)
    assert result.probability("put", 100) == pytest.approx(1 / 3)
    call = result.payoff_metrics("call", 100, 5)
    put = result.payoff_metrics("put", 110, 5)
    assert call is not None and put is not None
    assert call.expected_pnl == pytest.approx(5 / 3)
    assert call.loss_probability == pytest.approx(1 / 3)
    assert call.fifth_percentile_pnl == -5
    assert put.expected_pnl == pytest.approx(-5)
    assert put.loss_probability == pytest.approx(2 / 3)
    assert put.fifth_percentile_pnl == -15


def test_empirical_requires_30_disjoint_blocks_and_better_both_scores():
    too_few = [(i, i + 1, 1.0) for i in range(29)]
    evidence, selected = _empirical_evidence(too_few)
    assert selected is None
    assert evidence.rejection_reason == "insufficient_independent_blocks"

    shifted = [(i, i + 1, 1.0) for i in range(50)]
    evidence, selected = _empirical_evidence(shifted)
    assert evidence.independent_blocks == 50
    assert evidence.validation_blocks >= 10
    assert evidence.brier_delta < 0
    assert evidence.log_loss_delta < 0
    assert selected is not None

    # Even many overlapping origin labels provide only a few independent blocks.
    overlapping = [(i, i + 25, 1.0) for i in range(300)]
    evidence, selected = _empirical_evidence(overlapping)
    assert selected is None
    assert evidence.independent_blocks < 30

    support = _independent_samples([(i, i + 2, 0.0) for i in range(10)])
    validation = _independent_samples(
        [(i, i + 2, 0.0) for i in range(10)], embargo=True
    )
    assert [item[0] for item in support] == [0, 2, 4, 6, 8]
    assert [item[0] for item in validation] == [0, 3, 6, 9]


def test_rolling_evaluation_is_deterministic_and_ignores_future_bars():
    prefix = _bars(849)
    future = _bars(850, last=date(2026, 9, 25))
    original = evaluate_predictive_history(prefix, COMPLETED)
    extended = evaluate_predictive_history(future, COMPLETED)
    assert original == extended
    assert len(original["horizons"]) == 25
    first = original["horizons"][0]
    assert first["model_scores"] is not None
    assert first["baseline_comparator"] is not None
    assert first["forecast_coverage"]["attempted"] > 0
    assert first["forecast_coverage"]["coverage_rate"] == 1.0
    assert sum(bin_["count"] for bin_ in first["calibration"]) == (
        first["forecast_coverage"]["scored"]
    )


def test_evaluation_coverage_counts_rejections_and_missing_labels_by_moneyness():
    bars = _bars(200)
    # Index 101 is a maturity for a scored one-session origin; index 170 is an origin.
    missing = bars.filter(~pl.col("ts").is_in([bars["ts"][101], bars["ts"][170]]))
    report = evaluate_predictive_history(missing, COMPLETED)
    first = report["horizons"][0]
    coverage = first["forecast_coverage"]
    assert coverage["attempted"] > coverage["issued"] > coverage["scored"]
    assert coverage["rejected"] == coverage["attempted"] - coverage["issued"]
    assert coverage["unscored"] == coverage["issued"] - coverage["scored"]
    assert coverage["coverage_rate"] == pytest.approx(
        coverage["issued"] / coverage["attempted"]
    )
    assert sum(coverage["rejection_reasons"].values()) == coverage["rejected"]
    assert sum(coverage["unscored_reasons"].values()) == coverage["unscored"]
    assert coverage["unscored_reasons"]["maturity_close_missing"] > 0
    by_moneyness = first["by_moneyness"]
    for field in ("attempted", "issued", "rejected", "scored", "unscored"):
        assert sum(bucket[field] for bucket in by_moneyness.values()) == coverage[field]
    assert all(bucket["attempted"] > 0 for bucket in by_moneyness.values())


def test_evaluation_method_selection_uses_only_prior_matured_labels(monkeypatch):
    sessions = SessionCalendar().sessions(date(2026, 1, 1), date(2026, 3, 31))
    samples = [(i, i + 3, float(i)) for i in range(40)]
    selected = _prior_samples(samples, sessions, 30)
    assert selected
    assert max(item[1] for item in selected) == 29
    assert all(item[1] < 30 for item in selected)

    from stocksweeper.forecast import predictive

    seen = []
    original = predictive._prior_samples

    def checked_prior(matured, dates, origin):
        prior = original(matured, dates, origin)
        assert all(item[1] < origin for item in prior)
        seen.append(origin)
        return prior

    monkeypatch.setattr(predictive, "_prior_samples", checked_prior)
    evaluate_predictive_history(_bars(220), COMPLETED)
    assert seen


def test_forecaster_cache_evicts_prior_session_and_oldest_ticker(tmp_path):
    provider = Prices(_bars())
    forecaster = PredictiveForecaster(tmp_path, provider)
    forecaster.prepare("AAPL", COMPLETED)
    assert forecaster.forecast("AAPL", AS_OF, EXPIRY).status == "available"
    assert any(key[0] == "AAPL" for key in forecaster._candidates)
    next_session = date(2026, 9, 25)
    provider.frame = _bars(101, last=next_session)
    forecaster.prepare("AAPL", next_session)
    assert all(key[1] == next_session for key in forecaster._prepared)
    assert all(key[1] == next_session for key in forecaster._inputs)
    assert not forecaster._candidates
    assert forecaster.forecast(
        "AAPL", datetime(2026, 9, 25, 23, tzinfo=UTC), EXPIRY
    ).status == "available"
    for index in range(512):
        forecaster.forecast(f"T{index}", AS_OF, EXPIRY)
    assert len(forecaster._ticker_sessions) == 512
    assert "AAPL" not in forecaster._ticker_sessions
    assert not any(key[0] == "AAPL" for key in forecaster._prepared)
    assert not any(key[0] == "AAPL" for key in forecaster._inputs)
    assert not any(key[0] == "AAPL" for key in forecaster._candidates)


def test_unavailable_result_and_invalid_inputs_are_explicit(tmp_path):
    result = PredictiveForecaster(tmp_path, NoNetwork()).forecast("AAPL", AS_OF, EXPIRY)
    assert result.status == "unavailable"
    assert result.reason == "market_data_missing"
    assert result.probability("call", 100) is None
    with pytest.raises(ValueError, match="price"):
        result.probability("call", 0)
    with pytest.raises(ValueError, match="side"):
        result.probability("buy", 100)
    with pytest.raises(ValueError, match="timezone"):
        PredictiveForecaster(tmp_path).forecast("AAPL", datetime(2026, 9, 24), EXPIRY)
