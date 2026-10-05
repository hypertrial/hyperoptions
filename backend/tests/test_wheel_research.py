"""Adversarial synthetic checks for causal, assignment-neutral wheel research."""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.research.historical_options.artifacts import hash_file, verify_artifact
from stocksweeper.research.historical_options.snapshot import freeze_snapshot
from stocksweeper.research.historical_options.wheel import (
    _analyze,
    _condition,
    _conditional,
    _entry_rates,
    _period,
    _volatility_thresholds,
    _write_reports,
    assessment_days,
    depth_band,
    horizon_band,
    run_wheel,
    run_wheel_frames,
    select_wheel_contract,
    stock_features,
)
from stocksweeper.research.historical_options.wheel_statistics import (
    moving_block_interval,
    pareto_representatives,
    summarize,
    tail_stats,
)
from tests.test_historical_snapshot import research_sources as research_sources
from tests.test_historical_strategies import ENTRY, ORIGIN, _fixture


def _row(origin, value, *, days=1, baseline=0.0, traded=True):
    return {
        "origin": origin,
        "expiry_session": origin + timedelta(days=days),
        "return": value,
        "assessment_days": days,
        "baseline_return": baseline,
        "traded": traded,
    }


def _wheel_fixture(**kwargs):
    days, stocks, _hours = _fixture(**kwargs)
    known_opening = {row["session"]: row for row in
                     days.filter(pl.col("contract") == "p95").iter_rows(named=True)}
    alternatives = []
    for row in days.filter(pl.col("contract").is_in(["c95", "p95"])).iter_rows(named=True):
        call = row["side"] == "call"
        alternatives.append({**row, "contract": "c94" if call else "p94", "strike": 94.0,
                             "mark": 6.5 if call else 0.5, "opening_open": 1.2345,
                             "opening_start": known_opening[row["session"]]["opening_start"],
                             "opening_end": known_opening[row["session"]]["opening_end"]})
    days = pl.concat([days, pl.DataFrame(alternatives)])
    calendar = SessionCalendar()
    dates = calendar.sessions(date(2025, 5, 1), ORIGIN)
    history = pl.DataFrame({
        "ticker": ["CIFR"] * len(dates), "ts": list(dates),
        "open": [100.0] * len(dates), "high": [101.0] * len(dates),
        "low": [99.0] * len(dates), "close": [100.0] * len(dates),
        "volume": [1000.0] * len(dates), "dividends": [0.0] * len(dates),
        "stock_splits": [0.0] * len(dates),
    })
    return days, pl.concat([history, stocks.filter(pl.col("ts") == ENTRY)])


def _candidate(contract="a", *, strike=95, mark=6, side="call", tx=10, volume=20):
    return {
        "contract": contract, "strike": strike, "mark": mark, "side": side,
        "expiry_session": ENTRY, "transactions": tx, "volume": volume, "valid": True,
    }


@pytest.mark.parametrize(
    ("strike", "expected"),
    [(100, None), (100.01, None), (99.999, "0-2.5"), (97.5, "2.5-5"),
     (95, "5-10"), (90, "10-20"), (80, "20+"), (0, None), (-1, None)],
)
def test_strict_depth_and_half_open_band_boundaries(strike, expected):
    assert depth_band(100, strike) == expected


@pytest.mark.parametrize(
    ("horizon", "expected"),
    [(0, None), (1, "1"), (2, "2-5"), (5, "2-5"), (6, "6-10"),
     (10, "6-10"), (11, "11-25"), (25, "11-25"), (26, None)],
)
def test_horizon_bands_use_exact_session_boundaries(horizon, expected):
    assert horizon_band(horizon) == expected


def test_assessment_days_start_at_entry_and_include_expiry_session():
    assert assessment_days(ENTRY, ENTRY) == 1
    assert assessment_days(date(2025, 7, 3), date(2025, 7, 7)) == 5
    with pytest.raises(ValueError):
        assessment_days(date(2025, 7, 7), ENTRY)


@pytest.mark.parametrize(
    ("origin", "expiry", "expected"),
    [(date(2025, 9, 15), date(2025, 9, 16), "development"),
     (date(2025, 9, 16), date(2025, 9, 17), "purged"),
     (date(2025, 9, 16), date(2025, 9, 18), "purged"),
     (date(2025, 9, 17), date(2025, 9, 18), "holdout")],
)
def test_development_labels_mature_strictly_before_holdout(origin, expiry, expected):
    assert _period("primary", origin, expiry) == expected


def test_volatility_terciles_are_development_only_and_deduplicate_shared_dates():
    start = date(2025, 1, 2)
    development = [{"ticker": "CIFR", "origin": start + timedelta(days=index),
                    "period": "development", "volatility20": index / 10}
                   for index in range(6)]
    result = _volatility_thresholds(development)
    assert result["CIFR"] == pytest.approx(np.quantile(np.arange(6) / 10, [1 / 3, 2 / 3]))
    duplicated = [*development, *([development[0]] * 1000)]
    future = [{"ticker": "CIFR", "origin": date(2025, 9, 17),
               "period": "holdout", "volatility20": 9999},
              {"ticker": "CIFR", "origin": date(2026, 1, 2),
               "period": "supplement", "volatility20": -9999},
              {"ticker": "CIFR", "origin": date(2025, 9, 16),
               "period": "purged", "volatility20": 9999}]
    assert _volatility_thresholds([*duplicated, *future]) == result
    assert _volatility_thresholds(list(reversed([*duplicated, *future]))) == result


def test_condition_failures_retain_starting_state_and_unknown_features_remain_unknown():
    start = date(2025, 1, 2)
    rows = [{**_row(start, -0.02, baseline=-0.30), "ticker": "CIFR", "return20": -0.1},
            {**_row(start + timedelta(days=1), 0.03), "ticker": "CIFR", "return20": -0.1},
            {**_row(start + timedelta(days=2), 0.50), "ticker": "CIFR", "return20": None},
            {**_row(start + timedelta(days=3), 0.04), "ticker": "CIFR", "return20": 0.1}]
    result = _conditional(rows, "trend_positive", {})
    assert [row["return"] for row in result] == [-0.30, 0, 0.04]
    assert [row["traded"] for row in result] == [False, False, True]
    assert rows[0]["return"] == -0.02  # Comparison construction is not a source mutation.
    assert _condition({"return20": 0}, "trend_positive", {}) is False
    assert _condition({"return20": 0}, "trend_nonpositive", {}) is True


@pytest.mark.parametrize(
    ("tx", "volume", "passes"), [(4, 20, False), (5, 19, False), (5, 20, True)]
)
def test_activity_condition_has_exact_transaction_volume_boundaries(tx, volume, passes):
    row = {"decision_transactions": tx, "decision_volume": volume}
    assert _condition(row, "activity", {}) is passes


def test_volatility_condition_tercile_boundaries_and_missingness_are_explicit():
    thresholds = {"CIFR": [0.1, 0.2]}
    for value, expected in [(0.1, "low"), (0.2, "middle"), (0.21, "high")]:
        row = {"ticker": "CIFR", "volatility20": value}
        selected = [name for name in ("low", "middle", "high")
                    if _condition(row, f"volatility_{name}", thresholds)]
        assert selected == [expected]
    missing = {"ticker": "CIFR", "volatility20": None}
    assert _condition(missing, "volatility_low", thresholds) is None


def test_entry_rates_separate_known_cancellations_unknown_entries_and_other_exclusions():
    rows = [
        {"status": "scored", "reason": None},
        {"status": "no_entry", "reason": "opening_moneyness_changed"},
        {"status": "no_entry", "reason": "missing_opening_bucket"},
        {"status": "no_entry", "reason": "missing_verified_stock_open"},
        {"status": "excluded", "reason": "reconciliation_failure"},
    ]
    result = _entry_rates(rows)
    assert result == {
        "no_entry_rate": 3 / 5, "known_cancellation_rate": 1 / 5,
        "missing_entry_rate": 2 / 5,
    }
    assert result["no_entry_rate"] == pytest.approx(
        result["known_cancellation_rate"] + result["missing_entry_rate"]
    )
    assert _entry_rates([]) == {
        "no_entry_rate": None, "known_cancellation_rate": None, "missing_entry_rate": None,
    }


@pytest.mark.parametrize(
    "condition",
    ["volatility_low", "volatility_middle", "volatility_high", "trend_positive",
     "trend_nonpositive", "activity_5tx_20vol"],
)
def test_readable_checklist_supplies_exact_frozen_screening_thresholds(tmp_path, condition):
    metrics = {
        "return_per_day": 0.001, "expected_shortfall05": -0.05, "origin_dates": 20,
        "expiries": 4, "traded": 20, "loss_frequency": 0.1, "quantile05": -0.05,
        "participation": 1.0, "missing_entry_rate": 0.125,
        "known_cancellation_rate": 0.25, "no_entry_rate": 0.375,
        "decision_time_value_yield_quartiles": [0.01, 0.02, 0.03],
        "decision_breakeven_cushion_quartiles": [0.05, 0.10, 0.15],
    }
    recommendation = {
        "ticker": "CIFR", "side": "call", "preference": "balanced", "rule_id": "5-10/1",
        "depth_band": "5-10", "horizon_band": "1", "condition": condition,
        "development": metrics, "holdout": metrics, "supplement": metrics,
        "evidence": ["provisional"], "holdout_direction": "positive_net_reward",
        "cost_sensitivity": [{"period": "development", "return_per_day": 0.001}],
        "cost_sensitive_reward_sign": False,
    }
    _write_reports(tmp_path, {
        "recommendations": [recommendation], "rules": [], "counts": {},
        "volatility_thresholds": {"CIFR": [0.12345, 0.23456]},
    })
    for artifact in ("checklist.md", "report.md"):
        text = (tmp_path / artifact).read_text().lower()
        assert "missing-entry rate 12.50%" in text
        assert "known opening cancellation rate 25.00%" in text
        if condition.startswith("volatility_"):
            assert "annualized" in text and "realized volatility" in text and "20" in text
            # Either proportion or percentage units are acceptable; rounded terciles are not.
            numbers = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", text)]
            required = {
                "volatility_low": [0.12345], "volatility_middle": [0.12345, 0.23456],
                "volatility_high": [0.23456],
            }[condition]
            for cutoff in required:
                assert any(abs(number - cutoff) < 1e-10 or abs(number - 100 * cutoff) < 1e-8
                           for number in numbers)
            if condition == "volatility_high":
                assert ">" in text or "above" in text
            else:
                assert "≤" in text or "<=" in text or "at most" in text
        elif condition.startswith("trend_"):
            assert re.search(r"20[- ]session(?: stock)? return", text)
            if condition == "trend_positive":
                assert "> 0" in text or "positive" in text
            else:
                assert "≤ 0" in text or "<= 0" in text or "nonpositive" in text
        else:
            assert "transactions" in text and "5" in text
            assert ("volume" in text or "contracts" in text) and "20" in text
            assert "decision" in text or "previous" in text


def test_holdout_outcomes_cannot_choose_a_development_rule_or_screen(tmp_path):
    calendar = SessionCalendar()
    origins = calendar.sessions(date(2025, 1, 2), date(2025, 3, 1))[:24]
    rows = []
    for rule, values in (("2.5-5/1", [0.01] * 24), ("5-10/1", [-0.30, *([0.05] * 23)])):
        for origin, value in zip(origins, values, strict=True):
            rows.append({**_row(origin, value), "ticker": "CIFR", "side": "put",
                         "rule_id": rule, "period": "development", "status": "scored",
                         "volatility20": None, "return20": 0.0,
                         "decision_transactions": 10.0, "decision_volume": 100.0,
                         "expiry_itm": False, "haircut": 0.10})
    first_path, second_path = tmp_path / "a", tmp_path / "b"
    first_path.mkdir()
    second_path.mkdir()
    pl.DataFrame(rows).write_parquet(first_path / "cost_outcomes.parquet")
    first = _analyze(rows, first_path)
    holdout_origin = date(2025, 9, 17)
    changed = [*rows, *[
        {**rows[0], "origin": holdout_origin, "expiry_session": date(2025, 9, 18),
         "period": "holdout", "rule_id": rule, "return": value}
        for rule, value in (("2.5-5/1", -0.99), ("5-10/1", 100.0))
    ]]
    pl.DataFrame(changed).write_parquet(second_path / "cost_outcomes.parquet")
    second = _analyze(changed, second_path)
    def lock(summary):
        return [(row["ticker"], row["side"], row["preference"], row["rule_id"], row["condition"])
                for row in summary["recommendations"]]

    assert lock(first) == lock(second)
    chosen = {row["preference"]: row["rule_id"] for row in first["recommendations"]
              if row["ticker"] == "CIFR" and row["side"] == "put"}
    assert chosen["conservative"] == "2.5-5/1"
    assert chosen["higher_return"] == "5-10/1"


def test_matched_rule_comparisons_route_paired_dates_through_common_calendar_bootstrap(
    tmp_path, monkeypatch
):
    from stocksweeper.research.historical_options import wheel

    calendar = SessionCalendar()
    dates = calendar.sessions(date(2025, 1, 2), date(2025, 1, 10))[:4]
    rows = []
    for rule, observations in (
        ("2.5-5/1", [(dates[0], 0.01), (dates[1], 0.02), (dates[2], 99.0)]),
        ("5-10/1", [(dates[0], 0.02), (dates[1], 0.03), (dates[3], -99.0)]),
    ):
        for origin, value in observations:
            rows.append({**_row(origin, value), "ticker": "CIFR", "side": "put",
                         "rule_id": rule, "period": "development", "status": "scored",
                         "volatility20": None, "return20": 0.0,
                         "decision_transactions": 10.0, "decision_volume": 100.0,
                         "expiry_itm": False, "haircut": 0.10})
    pl.DataFrame(rows).write_parquet(tmp_path / "cost_outcomes.parquet")
    calls = []

    def paired_interval(left, schedule, paired_rows=None, **settings):
        calls.append((left, schedule, paired_rows))
        return moving_block_interval(left, schedule, paired_rows=paired_rows, **settings)

    monkeypatch.setattr(wheel, "moving_block_interval", paired_interval)
    summary = _analyze(rows, tmp_path)
    assert len(calls) == 1
    left, schedule, right = calls[0]
    assert [row["origin"] for row in left] == [row["origin"] for row in right] == list(dates[:2])
    assert schedule == calendar.sessions(date(2024, 10, 29), date(2025, 9, 16))
    comparison = summary["matched_comparisons"][0]
    assert comparison["matched_dates"] == 2
    assert comparison["left_metrics"]["mean_return"] == pytest.approx(0.015)
    assert comparison["right_metrics"]["mean_return"] == pytest.approx(0.025)
    inference = comparison["inference"]
    assert inference["status"] == "underpowered"
    assert inference["paired_origin_dates"] == 2
    assert inference["delta_return_per_day"] == pytest.approx(-0.01)
    assert inference["interval_delta_return_per_day"] is None
    assert inference["interval_delta_expected_shortfall05"] is None
    assert inference["bootstrap_draws"] == 2000
    assert inference["seed"] == 1729
    assert inference["block_length"] == 26
    assert inference["multiplicity"] == "exploratory"


def test_no_eligible_strict_moneyness_reports_no_supported_recommendation(tmp_path):
    days, stocks = _wheel_fixture()
    days = days.with_columns(pl.lit(100.0).alias("strike"))
    summary = run_wheel_frames(days, stocks, tmp_path)
    assert all(row["rule_id"] is None for row in summary["recommendations"])
    for name in ("candidates.parquet", "opportunities.parquet", "cost_outcomes.parquet"):
        assert pl.read_parquet(tmp_path / name).height == 0


@pytest.mark.parametrize("empty", ["stocks", "days"])
def test_empty_sources_are_rejected_explicitly_not_with_calendar_typeerror(tmp_path, empty):
    days, stocks = _wheel_fixture()
    with pytest.raises(ValueError, match="observations"):
        run_wheel_frames(days.clear() if empty == "days" else days,
                         stocks.clear() if empty == "stocks" else stocks, tmp_path)


def test_selection_ranks_decision_capped_reward_and_exact_activity_ties():
    candidates = [
        _candidate("z", tx=11, volume=20), _candidate("b", tx=11, volume=21),
        _candidate("a", tx=11, volume=21), _candidate("larger_volume", tx=10, volume=999),
        _candidate("huge_premium_but_intrinsic", strike=90, mark=11),
        _candidate("atm", strike=100, mark=1000),
    ]
    assert select_wheel_contract(candidates, 100, ENTRY)["contract"] == "a"
    assert select_wheel_contract(list(reversed(candidates)), 100, ENTRY)["contract"] == "a"
    # Total premium is not the capped CC reward: subtract intrinsic and the fee.
    assert select_wheel_contract(candidates[-2:], 100, ENTRY)["contract"] == (
        "huge_premium_but_intrinsic"
    )


@pytest.mark.parametrize("field", ["mark", "strike", "volume"])
@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf")])
def test_invalid_selection_observations_cannot_win(field, value):
    bad = {**_candidate("bad", mark=1000), field: value}
    assert select_wheel_contract([bad, _candidate("valid")], 100, ENTRY)["contract"] == "valid"


def test_selector_never_observes_future_prices_volume_or_original_cost_basis():
    candidates = [_candidate("best", mark=6.1), _candidate("other", mark=6)]
    chosen = select_wheel_contract(candidates, 100, ENTRY)
    changed = [{**row, "opening_open": -1, "entry_volume": 999999,
                "expiry_close": 0, "original_cost_basis": 0.001} for row in candidates]
    assert select_wheel_contract(changed, 100, ENTRY)["contract"] == chosen["contract"]


def test_features_use_contiguous_completed_history_without_future_leakage():
    calendar = SessionCalendar()
    dates = calendar.sessions(date(2025, 4, 1), ENTRY)
    close = np.exp(np.arange(len(dates)) * 0.003 + np.sin(np.arange(len(dates))) * 0.01)
    stocks = pl.DataFrame({"ticker": ["CIFR"] * len(dates), "ts": list(dates),
                           "close": close})
    result = stock_features(stocks)
    changed = stocks.with_columns(
        pl.when(pl.col("ts") > ORIGIN).then(9999.0).otherwise(pl.col("close")).alias("close")
    )
    assert result["CIFR", ORIGIN] == stock_features(changed)["CIFR", ORIGIN]
    features = result["CIFR", ORIGIN]
    index = dates.index(ORIGIN)
    assert features["return5"] == pytest.approx(close[index] / close[index - 5] - 1)
    assert features["return20"] == pytest.approx(close[index] / close[index - 20] - 1)
    assert features["volatility20"] is not None
    assert result["CIFR", dates[19]]["volatility20"] is None
    assert result["CIFR", dates[20]]["volatility20"] is not None
    missing = stocks.filter(pl.col("ts") != dates[index - 1])
    feature_gap = stock_features(missing)["CIFR", ORIGIN]
    assert feature_gap["return5"] is None
    assert feature_gap["return20"] is None
    assert feature_gap["volatility20"] is None


def test_zero_or_missing_close_breaks_feature_window_without_bridge_return():
    _days, stocks = _wheel_fixture()
    previous = SessionCalendar().offset(ORIGIN, -1)
    for invalid in (0, None, float("nan")):
        changed = stocks.with_columns(
            pl.when(pl.col("ts") == previous).then(invalid).otherwise(pl.col("close"))
            .alias("close")
        )
        feature = stock_features(changed)["CIFR", ORIGIN]
        assert feature["return5"] is None
        assert feature["return20"] is None
        assert feature["volatility20"] is None


def test_missing_opening_bucket_cannot_replace_selected_contract(tmp_path):
    days, stocks = _wheel_fixture(missing_opening=True)
    run_wheel_frames(days, stocks, tmp_path)
    opportunities = pl.read_parquet(tmp_path / "opportunities.parquet")
    chosen = opportunities.filter((pl.col("side") == "call") & (pl.col("depth_band") == "5-10"))
    assert chosen["contract"].unique().to_list() == ["c95"]
    assert chosen["status"].unique().to_list() == ["no_entry"]
    assert chosen["reason"].unique().to_list() == ["missing_opening_bucket"]
    assert chosen["return"].null_count() == chosen.height


@pytest.mark.parametrize("stock_open", [95.0, 94.0])
def test_next_open_must_preserve_strict_itm_call_and_otm_put_without_retargeting(
    tmp_path, stock_open
):
    days, stocks = _wheel_fixture()
    stocks = stocks.with_columns(
        pl.when(pl.col("ts") == ENTRY).then(stock_open).otherwise(pl.col("open")).alias("open")
    )
    run_wheel_frames(days, stocks, tmp_path)
    opportunities = pl.read_parquet(tmp_path / "opportunities.parquet")
    selected = opportunities.filter(pl.col("depth_band") == "5-10")
    assert set(selected["side"].to_list()) == {"call", "put"}
    assert set(selected["contract"].to_list()) == {"c95", "p95"}
    assert selected["status"].unique().to_list() == ["no_entry"]
    assert selected["reason"].unique().to_list() == ["opening_moneyness_changed"]
    assert selected["traded"].to_list() == [False] * selected.height
    for row in selected.iter_rows(named=True):
        expected = 104 / stock_open - 1 if row["side"] == "call" else 0
        assert row["return"] == pytest.approx(expected)


def test_moneyness_skip_retains_owned_stock_or_cash_in_whole_opportunity_statistics(tmp_path):
    days, stocks = _wheel_fixture()
    stocks = stocks.with_columns(
        pl.when(pl.col("ts") == ENTRY).then(90.0).otherwise(pl.col("open")).alias("open"),
        pl.when(pl.col("ts") == ENTRY).then(70.0).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.col("ts") == ENTRY).then(65.0).otherwise(pl.col("low")).alias("low"),
    )
    summary = run_wheel_frames(days, stocks, tmp_path)
    selected = pl.read_parquet(tmp_path / "opportunities.parquet").filter(
        pl.col("depth_band") == "5-10"
    )
    assert set(selected["contract"].to_list()) == {"c95", "p95"}
    assert selected["status"].unique().to_list() == ["no_entry"]
    assert selected["reason"].unique().to_list() == ["opening_moneyness_changed"]
    for row in selected.iter_rows(named=True):
        call = row["side"] == "call"
        expected_return, capital = (-2 / 9, 9000) if call else (0, 9500)
        assert row["traded"] is False
        assert row["return"] == pytest.approx(expected_return)
        assert row["baseline_return"] == pytest.approx(expected_return)
        assert row["capital"] == capital
        assert row["net_outlay"] == capital
        assert row["net_premium"] == row["fee"] == 0
        assert Decimal(row["pnl_decimal"]) == (-2000 if call else 0)
        assert row["ending_shares"] == (100 if call else 0)
        assert row["ending_share_value"] == (7000 if call else 0)
        assert row["ending_cash"] == (0 if call else 9500)
        assert row["ending_cash"] + row["ending_share_value"] == pytest.approx(
            capital * (1 + expected_return)
        )
        rule = next(rule for rule in summary["rules"] if rule["side"] == row["side"]
                    and rule["rule_id"] == "5-10/1" and rule["period"] == "development")
        assert rule["origin_dates"] == 1
        assert rule["traded"] == 0
        assert rule["return_per_day"] == pytest.approx(expected_return)
        assert rule["participation"] == 0
        assert rule["known_cancellation_rate"] == rule["no_entry_rate"] == 1
        assert rule["missing_entry_rate"] == 0
        assert rule["traded_leg"]["scored"] == 0
        # Applying no condition or a passing condition must never invent an option sale.
        for condition in (None, "activity"):
            conditioned = _conditional([row], condition, {})
            assert conditioned[0]["traded"] is False
            assert conditioned[0]["return"] == pytest.approx(expected_return)
        stressed = pl.read_parquet(tmp_path / "cost_outcomes.parquet").filter(
            pl.col("contract") == row["contract"]
        )
        assert stressed["fee"].unique().to_list() == [0]
        assert stressed["net_premium"].unique().to_list() == [0]
        assert stressed["return"].unique().to_list() == [pytest.approx(expected_return)]
    assert all(rec["rule_id"] is None for rec in summary["recommendations"])


@pytest.mark.parametrize("unavailable", ["expiry_close", "unknown_action", "split"])
def test_unsafe_moneyness_skips_do_not_become_known_cash_or_stock_returns(tmp_path, unavailable):
    days, stocks = _wheel_fixture()
    updates = [pl.when(pl.col("ts") == ENTRY).then(90.0)
               .otherwise(pl.col("open")).alias("open")]
    column, value = {
        "expiry_close": ("close", None),
        "unknown_action": ("stock_splits", None),
        "split": ("stock_splits", 2.0),
    }[unavailable]
    updates.append(pl.when(pl.col("ts") == ENTRY).then(value)
                   .otherwise(pl.col(column)).alias(column))
    run_wheel_frames(days, stocks.with_columns(updates), tmp_path)
    selected = pl.read_parquet(tmp_path / "opportunities.parquet").filter(
        pl.col("depth_band") == "5-10"
    )
    assert selected["return"].null_count() == selected.height
    assert selected["baseline_return"].null_count() == selected.height
    assert True not in selected["traded"].to_list()


def test_passive_moneyness_skip_preserves_fractional_cash_and_share_wealth(tmp_path):
    days, stocks = _wheel_fixture()
    days = days.filter(pl.col("contract").is_in(["c95", "p95"])).with_columns(
        pl.lit(95.012375).alias("strike")
    )
    stocks = stocks.with_columns(
        pl.when(pl.col("ts") == ENTRY).then(90.12345).otherwise(pl.col("open")).alias("open"),
        pl.when(pl.col("ts") == ENTRY).then(70.6789).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.col("ts") == ENTRY).then(65.0).otherwise(pl.col("low")).alias("low"),
    )
    run_wheel_frames(days, stocks, tmp_path)
    selected = pl.read_parquet(tmp_path / "opportunities.parquet")
    call, put = (selected.filter(pl.col("side") == side).row(0, named=True)
                 for side in ("call", "put"))
    assert call["ending_share_value"] == pytest.approx(7067.89)
    assert Decimal(call["pnl_decimal"]) == Decimal("-1944.455")
    assert put["ending_cash"] == pytest.approx(9501.2375)
    assert Decimal(put["capital_decimal"]) == Decimal("9501.2375")
    assert put["net_premium"] == 0


def test_positive_passive_stock_opportunities_cannot_be_a_supported_selling_rule(tmp_path):
    origins = SessionCalendar().sessions(date(2025, 1, 2), date(2025, 3, 1))[:24]
    rows = [
        {**_row(origin, 0.10, baseline=0.10, traded=False), "ticker": "CIFR", "side": "call",
         "rule_id": "5-10/1", "period": "development", "status": "no_entry",
         "reason": "opening_moneyness_changed", "volatility20": None, "return20": 0.0,
         "decision_transactions": 10.0, "decision_volume": 100.0, "expiry_itm": False,
         "haircut": 0.10, "fee": 0.0}
        for origin in origins
    ]
    pl.DataFrame(rows).write_parquet(tmp_path / "cost_outcomes.parquet")
    summary = _analyze(rows, tmp_path)
    rule = next(row for row in summary["rules"] if row["ticker"] == "CIFR")
    assert rule["origin_dates"] == 24
    assert rule["return_per_day"] == pytest.approx(0.10)
    assert rule["participation"] == 0
    assert rule["traded_leg"]["origin_dates"] == 0
    assert all(rec["rule_id"] is None for rec in summary["recommendations"])


def test_original_purchase_price_and_unrealized_profit_do_not_change_next_leg(tmp_path):
    days, stocks = _wheel_fixture()
    run_wheel_frames(days, stocks, tmp_path / "a")
    changed = stocks.with_columns(pl.lit(0.001).alias("original_purchase_price"),
                                  pl.lit(1000000.0).alias("unrealized_pnl"))
    run_wheel_frames(days, changed, tmp_path / "b")
    for name in ("opportunities.parquet", "cost_outcomes.parquet"):
        assert pl.read_parquet(tmp_path / "a" / name).equals(pl.read_parquet(tmp_path / "b" / name))


def test_future_entry_activity_and_maturity_cannot_change_decision_features(tmp_path):
    days, stocks = _wheel_fixture()
    run_wheel_frames(days, stocks, tmp_path / "a")
    later_days = days.with_columns(
        pl.when(pl.col("session") > ORIGIN).then(999999.0).otherwise(pl.col("mark")).alias("mark"),
        pl.when(pl.col("session") > ORIGIN).then(1.0).otherwise(pl.col("volume")).alias("volume"),
        pl.when(pl.col("session") > ORIGIN).then(222.0)
        .otherwise(pl.col("opening_open")).alias("opening_open"),
    )
    later_stocks = stocks.with_columns(
        pl.when(pl.col("ts") > ORIGIN).then(900.0).otherwise(pl.col("close")).alias("close")
    )
    run_wheel_frames(later_days, later_stocks, tmp_path / "b")
    first = pl.read_parquet(tmp_path / "a" / "opportunities.parquet")
    second = pl.read_parquet(tmp_path / "b" / "opportunities.parquet")
    columns = ["origin", "contract", "side", "depth_band", "horizon_band",
               "volatility20", "return5", "return20",
               *[name for name in first.columns if name.startswith("decision_")]]
    assert first.select(columns).equals(second.select(columns))


def test_reference_accounting_uses_current_wealth_fractional_prices_and_same_day_expiry(tmp_path):
    days, stocks = _wheel_fixture()
    run_wheel_frames(days, stocks, tmp_path)
    opportunities = pl.read_parquet(tmp_path / "opportunities.parquet")
    selected = opportunities.filter(pl.col("depth_band") == "5-10")
    assert selected["assessment_days"].unique().to_list() == [1]
    # July 4 holiday maps to July 3, also the next-session entry and early close.
    assert selected["entry_session"].to_list() == selected["expiry_session"].to_list()
    for row in selected.iter_rows(named=True):
        spot, strike, maturity = Decimal("100.12345"), Decimal(95), Decimal(104)
        net_premium = Decimal("1.2345") * Decimal("0.9") * 100 - Decimal("0.65")
        capital = 100 * (spot if row["side"] == "call" else strike)
        terminal = 100 * (min(maturity, strike) if row["side"] == "call" else (
            strike - max(strike - maturity, Decimal(0))
        )) + net_premium
        assert row["return"] == pytest.approx(float((terminal - capital) / capital))
        expected_baseline = float((maturity - spot) / spot) if row["side"] == "call" else 0
        assert row["baseline_return"] == pytest.approx(expected_baseline)


@pytest.mark.parametrize("maturity", [90.0, 95.0, 104.0])
def test_expiry_wealth_counts_acquired_or_retained_shares_at_market_value(tmp_path, maturity):
    days, stocks = _wheel_fixture()
    stocks = stocks.with_columns(
        pl.when(pl.col("ts") == ENTRY).then(maturity).otherwise(pl.col("close")).alias("close")
    )
    run_wheel_frames(days, stocks, tmp_path)
    rows = pl.read_parquet(tmp_path / "opportunities.parquet").filter(
        pl.col("depth_band") == "5-10"
    )
    for row in rows.iter_rows(named=True):
        stock, strike, close = Decimal("100.12345"), Decimal(95), Decimal(str(maturity))
        net_premium = Decimal("1.2345") * Decimal("0.9") * 100 - Decimal("0.65")
        starting_capital = 100 * (stock if row["side"] == "call" else strike)
        if row["side"] == "call":
            # Unassigned calls keep 100 shares; assigned calls receive strike cash.
            terminal_value = 100 * min(close, strike) + net_premium
        else:
            # Assigned puts own 100 shares; strike cash funds that acquisition.
            terminal_value = 100 * min(close, strike) + net_premium
        assert row["return"] == pytest.approx(float(
            (terminal_value - starting_capital) / starting_capital
        ))
        assert row["ending_cash"] + row["ending_share_value"] == pytest.approx(
            float(terminal_value)
        )
        assert row["ending_share_value"] == pytest.approx(row["ending_shares"] * maturity)
        assert row["expiry_atm"] is (maturity == 95)
        assert row["expiry_itm"] is (
            maturity > 95 if row["side"] == "call" else maturity < 95
        )
        if maturity == 95:
            assert row["ending_state"] == "atm_ambiguous"
        else:
            expected_shares = maturity < 95
            assert row["ending_shares"] == (100 if expected_shares else 0)
            assert row["ending_state"] == ("shares" if expected_shares else "cash")


def test_fees_exceeding_premium_remain_negative_and_exact_breakeven_is_not_loss(tmp_path):
    days, stocks = _wheel_fixture()
    days = days.with_columns(
        pl.when((pl.col("session") == ENTRY) & (pl.col("contract") == "p95"))
        .then(0.00125).otherwise(pl.col("opening_open")).alias("opening_open")
    )
    run_wheel_frames(days, stocks, tmp_path / "negative")
    puts = pl.read_parquet(tmp_path / "negative" / "opportunities.parquet").filter(
        pl.col("contract") == "p95"
    )
    assert puts["return"].to_list() == [pytest.approx(-0.5375 / 9500)]
    days = days.with_columns(
        pl.when((pl.col("session") == ENTRY) & (pl.col("contract") == "c95"))
        .then(5.5625).otherwise(pl.col("opening_open")).alias("opening_open")
    )
    stocks = stocks.with_columns(
        pl.when(pl.col("ts") == ENTRY).then(99.99975).otherwise(pl.col("open")).alias("open")
    )
    run_wheel_frames(days, stocks, tmp_path / "breakeven")
    calls = pl.read_parquet(tmp_path / "breakeven" / "opportunities.parquet").filter(
        pl.col("contract") == "c95"
    )
    assert calls["return"].to_list() == [0.0]


def test_cost_sensitivities_keep_selection_fixed_and_do_not_repeat_put_stock_slippage(tmp_path):
    days, stocks = _wheel_fixture()
    run_wheel_frames(days, stocks, tmp_path)
    costs = pl.read_parquet(tmp_path / "cost_outcomes.parquet")
    assert set(costs["haircut"].to_list()) == {0.0, 0.05, 0.10, 0.20}
    for _, group in costs.group_by("opportunity_id"):
        assert group["contract"].n_unique() == 1
    puts = costs.filter(pl.col("side") == "put")
    assert puts["stock_slippage_bps"].unique().to_list() == [0]


def test_reconciliation_and_later_actions_exclude_without_replacement(tmp_path):
    for label, kwargs in (("reconcile", {"reconcile": True}), ("split", {"split": 2.0}),
                          ("dividend", {"dividend": 0.10})):
        days, stocks = _wheel_fixture(**kwargs)
        run_wheel_frames(days, stocks, tmp_path / label)
        rows = pl.read_parquet(tmp_path / label / "opportunities.parquet")
        assert rows["status"].unique().to_list() == ["excluded"]
        assert rows["return"].null_count() == rows.height
        assert rows.filter((pl.col("side") == "call") & (pl.col("depth_band") == "5-10"))[
            "contract"
        ].unique().to_list() == ["c95"]


def test_tail_statistics_allocate_fractional_boundary_mass_and_keep_atoms():
    values = [-1.0, -0.2, *([0.1] * 19)]
    result = tail_stats(values)
    # 21 equally weighted observations have 1.05 observations in their worst 5%.
    assert result["quantile05"] == -0.2
    assert result["expected_shortfall05"] == pytest.approx((-1.0 - 0.2 * 0.05) / 1.05)
    assert result["tail_loss"] == pytest.approx(-result["expected_shortfall05"])
    assert result == tail_stats(list(reversed(values)))
    atom = tail_stats([0.125] * 40)
    assert atom["quantile05"] == pytest.approx(0.125)
    assert atom["expected_shortfall05"] == pytest.approx(0.125)
    assert atom["tail_loss"] == 0


def test_weighted_tail_is_scale_invariant_and_uses_partial_weight_at_quantile():
    values, weights = [-1.0, -0.2, 0.1], [1.0, 1.0, 98.0]
    result = tail_stats(values, weights)
    assert result["quantile05"] == 0.1
    assert result["expected_shortfall05"] == pytest.approx((-1.0 - 0.2 + 3 * 0.1) / 5)
    assert result == tail_stats(values, [weight * 7 for weight in weights])
    tied = tail_stats([-1.0, -1.0, 0.3], [1.0, 3.0, 96.0])
    assert tied["expected_shortfall05"] == pytest.approx((-4 + 0.3) / 5)
    assert tied == tail_stats([-1.0, 0.3], [4.0, 96.0])


def test_empty_tail_and_zero_weight_observations_do_not_become_losses():
    assert tail_stats([]) == {
        "quantile05": None, "expected_shortfall05": None, "tail_loss": None,
    }
    assert tail_stats([-100.0, 0.125], [0, 1]) == tail_stats([0.125])
    assert summarize([])["return_per_day"] is None


@pytest.mark.parametrize(
    ("values", "weights", "alpha"),
    [
        ([float("nan")], None, 0.05),
        ([float("inf")], None, 0.05),
        ([1.0, 2.0], [1.0], 0.05),
        ([1.0], [-1.0], 0.05),
        ([1.0], [0.0], 0.05),
        ([1.0], [float("nan")], 0.05),
        ([1.0], None, 0),
        ([1.0], None, 1.01),
    ],
)
def test_invalid_tail_inputs_do_not_produce_plausible_scores(values, weights, alpha):
    with pytest.raises(ValueError):
        tail_stats(values, weights, alpha)


def test_capital_day_reward_is_ratio_of_sums_not_mean_daily_apr():
    start = date(2025, 1, 2)
    result = summarize(
        [_row(start, 0.10, days=1, baseline=0.03),
         _row(start + timedelta(days=1), 0.02, days=9, baseline=-0.01)]
    )
    assert result["mean_return"] == pytest.approx(0.06)
    assert result["total_assessment_days"] == 10
    assert result["return_per_day"] == pytest.approx(0.012)
    assert result["excess_return_per_day"] == pytest.approx(0.01)
    assert result["loss_frequency"] == 0


def test_no_trade_has_owned_share_or_cash_baseline_and_is_not_a_lossless_call():
    start = date(2025, 1, 2)
    calls = summarize([_row(start, -0.30, baseline=-0.30, traded=False)])
    puts = summarize([_row(start, 0, baseline=0, traded=False)])
    assert calls["traded"] == puts["traded"] == 0
    assert calls["loss_frequency"] == 1
    assert calls["expected_shortfall05"] == -0.30
    assert calls["excess_return_per_day"] == puts["excess_return_per_day"] == 0
    assert puts["return_per_day"] == 0


@pytest.mark.parametrize("days", [0, -1, 1.5])
def test_invalid_assessment_days_rejected(days):
    with pytest.raises(ValueError):
        summarize([_row(date(2025, 1, 2), 0.01, days=days)])


@pytest.mark.parametrize("field", ["return", "baseline_return"])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_unknown_or_invalid_outcome_cannot_be_coerced_to_intentional_no_trade(field, value):
    row = {**_row(date(2025, 1, 2), 0.01), field: value}
    with pytest.raises(ValueError):
        summarize([row])


def test_duplicate_shared_origin_cannot_inflate_support():
    row = _row(date(2025, 1, 2), 0.01)
    with pytest.raises(ValueError, match="duplicate"):
        summarize([row, dict(row)])


@pytest.mark.parametrize(
    ("calendar_count", "origin_count"), [(26 * 8, 19), (26 * 8 - 1, 26 * 8 - 1)]
)
def test_date_and_complete_block_requirements_both_gate_inference(calendar_count, origin_count):
    calendar = [date(2025, 1, 2) + timedelta(days=index) for index in range(calendar_count)]
    origins = calendar[:origin_count]
    result = moving_block_interval([_row(origin, 0.01) for origin in origins], calendar, draws=32)
    assert result["status"] == "underpowered"
    assert result["interval_return_per_day"] is None
    assert result["interval_expected_shortfall05"] is None


def test_paired_block_bootstrap_preserves_matching_offsets_and_row_order():
    calendar = [date(2025, 1, 2) + timedelta(days=index) for index in range(26 * 8)]
    left = [_row(origin, 0.02 + (index % 7) * 0.001) for index, origin in enumerate(calendar)]
    right = [{**row, "return": row["return"] - 0.03} for row in left]
    result = moving_block_interval(left, calendar, paired_rows=right, draws=64)
    assert result["status"] == "estimable"
    assert result["paired_origin_dates"] == len(calendar)
    assert result["delta_return_per_day"] == pytest.approx(0.03)
    assert result["interval_delta_return_per_day"] == pytest.approx([0.03, 0.03])
    assert result["delta_expected_shortfall05"] == pytest.approx(0.03)
    assert result["interval_delta_expected_shortfall05"] == pytest.approx([0.03, 0.03])
    assert result == moving_block_interval(
        list(reversed(left)), calendar, paired_rows=list(reversed(right)), draws=64
    )


def test_pairing_excludes_unmatched_dates_and_does_not_fill_unknown_with_cash():
    calendar = [date(2025, 1, 2) + timedelta(days=index) for index in range(26 * 8)]
    left = [_row(origin, 0.01) for origin in calendar]
    right = [_row(origin, 0.01) for origin in calendar[:19]]
    result = moving_block_interval(left, calendar, paired_rows=right, draws=32)
    assert result["paired_origin_dates"] == 19
    assert result["status"] == "underpowered"
    assert result["interval_delta_return_per_day"] is None


def test_calendar_gap_blocks_do_not_invent_independent_support():
    calendar = [date(2025, 1, 2) + timedelta(days=index) for index in range(26 * 8)]
    origins = [*calendar[:20], *calendar[26:27]]
    result = moving_block_interval([_row(origin, 0.01) for origin in origins], calendar, draws=32)
    assert result["origin_dates"] == 21
    assert result["calendar_complete_blocks"] == 8
    assert result["complete_blocks"] == 2
    assert result["status"] == "underpowered"


@pytest.mark.parametrize("schedule", ["reverse", "duplicate", "missing_origin"])
def test_invalid_bootstrap_calendar_cannot_silently_reorder_or_drop_dates(schedule):
    origin = date(2025, 1, 2)
    dates = [origin, origin + timedelta(days=1)]
    if schedule == "reverse":
        dates.reverse()
    elif schedule == "duplicate":
        dates.append(origin + timedelta(days=1))
    else:
        dates = dates[1:]
    with pytest.raises(ValueError):
        moving_block_interval([_row(origin, 0.01)], dates, draws=32)


def test_frontier_representatives_require_support_reward_and_non_dominance():
    def point(rule, reward, tail, *, origins=20, expiries=4):
        return {
            "rule_id": rule, "return_per_day": reward, "tail_loss": tail,
            "origin_dates": origins, "expiries": expiries, "loss_frequency": 0.1,
        }

    result = pareto_representatives([
        point("safe", 0.001, 0.05), point("middle", 0.002, 0.10),
        point("reward", 0.003, 0.20), point("dominated", 0.0015, 0.15),
        point("underpowered", 0.10, 0.0, origins=19),
        point("few_expiries", 0.10, 0.0, expiries=3), point("zero", 0, 0),
    ])
    assert result == {"conservative": "safe", "balanced": "middle", "higher_return": "reward"}
    assert pareto_representatives([point("losing", -0.01, 0)]) == {
        "conservative": None, "balanced": None, "higher_return": None,
    }


def test_wheel_replay_is_offline_read_only_immutable_and_deterministic(
    research_sources, tmp_path, monkeypatch
):
    from stocksweeper.research.historical_options import runner

    def forbidden(*_args, **_kwargs):
        pytest.fail("wheel research attempted network or reran the forecaster contest")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(runner, "run_snapshot", forbidden)
    db, prices, flat = research_sources
    source_files = [db, *prices.iterdir(), *flat.iterdir()]
    before = {path: hash_file(path) for path in source_files}
    frozen = tmp_path / "frozen snapshot"
    freeze_snapshot(*research_sources, frozen)
    frozen_before = {path.name: hash_file(path) for path in frozen.iterdir()}
    first = run_wheel(frozen, tmp_path / "first")
    second = run_wheel(frozen, tmp_path / "second")
    assert first["canonical_hash"] == second["canonical_hash"]
    assert verify_artifact(tmp_path / "first")["canonical_hash"] == first["canonical_hash"]
    assert before == {path: hash_file(path) for path in source_files}
    assert frozen_before == {path.name: hash_file(path) for path in frozen.iterdir()}
    summary = (tmp_path / "first" / "summary.json").read_text()
    assert str(tmp_path) not in summary
    assert "duration_seconds" not in summary
    assert json.loads(summary)["recommendations"]
    assert (tmp_path / "first" / "report.md").is_file()


def test_wheel_publication_rejects_conflicts_overlap_and_corrupt_snapshot(
    research_sources, tmp_path
):
    frozen = tmp_path / "snapshot"
    freeze_snapshot(*research_sources, frozen)
    for output in (frozen, frozen / "nested", tmp_path):
        with pytest.raises(ValueError, match="overlap"):
            run_wheel(frozen, output)
    occupied = tmp_path / "existing"
    occupied.mkdir()
    sentinel = occupied / "preserve.txt"
    sentinel.write_text("untouched")
    with pytest.raises(FileExistsError):
        run_wheel(frozen, occupied)
    assert sentinel.read_text() == "untouched"
    with (frozen / "days.parquet").open("ab") as stream:
        stream.write(b"corruption")
    output = tmp_path / "corrupted run"
    with pytest.raises(ValueError, match="hash mismatch"):
        run_wheel(frozen, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".corrupted run.*"))


def test_wheel_failure_before_report_publication_leaves_no_completed_destination(
    research_sources, tmp_path, monkeypatch
):
    from stocksweeper.research.historical_options import wheel

    frozen, output = tmp_path / "snapshot", tmp_path / "run"
    freeze_snapshot(*research_sources, frozen)

    def fail_analysis(_rows, stage):
        assert (stage / "candidates.parquet").is_file()
        assert (stage / "opportunities.parquet").is_file()
        assert (stage / "cost_outcomes.parquet").is_file()
        raise RuntimeError("injected wheel analysis failure")

    monkeypatch.setattr(wheel, "_analyze", fail_analysis)
    with pytest.raises(RuntimeError, match="injected wheel"):
        run_wheel(frozen, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".run.*"))
    verify_artifact(frozen)


def test_wheel_command_accepts_frozen_paths_with_spaces_and_emits_manifest_hash(
    research_sources, tmp_path, monkeypatch, capsys
):
    for name in ("POLARS_MAX_THREADS", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                 "VECLIB_MAXIMUM_THREADS"):
        monkeypatch.setenv(name, "1")
    script = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_historical_options.py"
    spec = importlib.util.spec_from_file_location("wheel_cli_test", script)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    frozen, output = tmp_path / "frozen with spaces", tmp_path / "wheel with spaces"
    freeze_snapshot(*research_sources, frozen)
    monkeypatch.setattr(sys, "argv", [str(script), "wheel", "--snapshot", str(frozen),
                                    "--output", str(output)])
    cli.main()
    emitted = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert emitted["canonical_hash"] == verify_artifact(output)["canonical_hash"]
    assert (output / "opportunities.parquet").is_file()


def test_wheel_artifact_paths_reject_symlinked_output_ancestors(research_sources, tmp_path):
    frozen = tmp_path / "snapshot"
    freeze_snapshot(*research_sources, frozen)
    actual = tmp_path / "actual"
    actual.mkdir()
    symlink = tmp_path / "alias"
    symlink.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        run_wheel(frozen, symlink / "run")
    assert not list(actual.iterdir())
