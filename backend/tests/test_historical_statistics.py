from datetime import date

import numpy as np
import polars as pl

from stocksweeper.research.historical_options.statistics import (
    holdout_boundary, inference_schedule, paired_interval,
)


def test_schedule_fixed_across_availability_and_purged_quarter():
    start, end = date(2024, 10, 29), date(2025, 12, 31)
    schedule = inference_schedule(start, end, 26)
    assert schedule[0] == start
    assert len(schedule) == 12
    boundary = holdout_boundary(start, end)
    assert date(2025, 9, 1) < boundary < date(2025, 10, 1)
    assert len([d for d in schedule if d >= boundary]) < 20


def test_insufficient_support_is_explicit_even_with_many_tickers():
    frame = pl.DataFrame({"origin": [date(2025, 1, 2)] * 100,
                          "delta": np.linspace(-1, 1, 100)})
    result = paired_interval(frame, adjusted=True)
    assert result["paired_units"] == 100
    assert result["paired_dates"] == 1
    assert result["interval"] is None
    assert result["status"] == "not_estimable"
    assert result["comparison_count"] == 7


def test_cluster_bootstrap_keeps_tickers_paired_and_is_deterministic():
    dates = inference_schedule(date(2025, 1, 2), date(2025, 5, 1), 2)[:20]
    frame = pl.DataFrame({"origin": [d for d in dates for _ in range(2)],
                          "delta": [-1., 1.] * 20})
    result = paired_interval(frame)
    assert result == paired_interval(frame.reverse())
    assert result["status"] == "estimable"
    assert result["interval"] == [0., 0.]
    assert result["multiplicity"] == "exploratory"


def test_paired_metrics_dedupe_shared_close_and_match_own_baseline(tmp_path):
    from stocksweeper.research.historical_options.statistics import forecast_inference

    start = date(2024, 10, 29)
    rows = []
    for model, contract, brier in [
        ("lognormal_ewma", "a", .1), ("lognormal_ewma", "b", .9),
        ("challenger", "a", .2), ("challenger", "a", .2),
        ("another", "b", .8),
    ]:
        rows.append({"panel": "primary", "ticker": "CIFR", "origin": start,
                     "expiry_session": date(2024, 10, 30), "horizon": 1,
                     "contract": contract, "model": model, "brier": brier,
                     "primary_eligible": True})
    path = tmp_path / "cells.parquet"
    pl.DataFrame(rows).write_parquet(path)
    results = forecast_inference(path)
    eligible = [r for r in results if r["panel"] == "primary"
                and r["band"] == "1" and r["period"] == "development"]
    challenger = next(r for r in eligible if r["model"] == "challenger")
    other = next(r for r in eligible if r["model"] == "another")
    assert challenger["baseline"] == .1
    assert challenger["paired_delta"] == .1
    assert challenger["matched_contract_cells"] == 1
    assert other["baseline"] == .9
    assert other["paired_units"] == 1
    assert other["status"] == "not_estimable"


def test_expected_pnl_validation_uses_paired_outcomes(tmp_path):
    from stocksweeper.research.historical_options.statistics import forecast_descriptives

    path = tmp_path / "cells.parquet"
    pl.DataFrame({"panel": ["primary"]*2, "model": ["challenger"]*2,
                  "side": ["call"]*2, "horizon": [1]*2,
                  "primary_eligible": [True, False], "expected_pnl": [1., 100.],
                  "realized_pnl": [2., None]}).write_parquet(path)
    row = next(r for r in forecast_descriptives(path)
               if r["quality"] == "reconciliation_inclusion")
    assert row["pnl_paired_cells"] == 1
    assert row["paired_expected_pnl_mean"] == 1
    assert row["paired_realized_pnl_mean"] == 2
    assert row["paired_pnl_bias"] == -1
