"""Fresh-process reproducibility of grouped premium-mark sensitivity reports."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from itertools import product
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.research.historical_options.calibration import FORECAST_SCHEMA
from stocksweeper.research.historical_options.statistics import PANELS, holdout_boundary
from stocksweeper.research.historical_options.strategies import (
    OUTCOME_SCHEMA,
    TIMING_SCHEMA,
    _timing_summary,
)

GROUP_KEYS = ("panel", "quality", "screen", "policy", "side", "mark_hour_et", "status")


def _adversarial_timing_archive(path: Path):
    """Interleave cells and cancellation across many differently aligned row groups."""
    groups = list(
        product(
            ("primary", "supplement"),
            ("primary", "reconciliation_inclusion"),
            ("unscreened", "previous_volume_20"),
            ("maximum_apr", "near_atm", "five_percent_below", "wider_comparator"),
            ("call", "put"),
            (9, 10, 13, 15),
        )
    )
    # Bounded returns with cancellation, fractional ulps, and missing observations.
    values = (0.1 + 2**-55, -0.1, 2**-57, 0.125, -0.125, None, 1e-8, -1e-8, 2**-55, 0.0)
    rows = []
    expected = {}
    for observation in range(513):
        for index, group in enumerate(groups):
            status = "missing_mark" if index == 1 else "scored"
            key = (*group, status)
            if index == 0:
                value = 2**-60  # A valid mean must not be rounded to zero.
            elif index == 1:
                value = None  # Every missing mark still belongs in its denominator.
            else:
                value = values[(observation + index) % len(values)]
            rows.append(
                {
                    **dict(zip(GROUP_KEYS, key, strict=True)),
                    "return": value,
                    "interpretation": "premium-mark sensitivity; fixed stock reference",
                }
            )
            expected.setdefault(key, []).append(value)
    table = pa.Table.from_pylist(rows, schema=TIMING_SCHEMA)
    assert table.num_rows > 100_000
    pq.write_table(table, path, compression="zstd", row_group_size=4093)
    assert pq.ParquetFile(path).metadata.num_row_groups > 30
    return expected


def _fresh_process_summary(source: Path, target: Path, seed: int):
    backend = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(POLARS_MAX_THREADS="1", PYTHONHASHSEED=str(seed))
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(backend / "src"), environment.get("PYTHONPATH", ""))
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from stocksweeper.research.historical_options.strategies import _timing_summary; "
            "_timing_summary(Path(sys.argv[1])).write_parquet(sys.argv[2], compression='zstd')",
            str(source),
            str(target),
        ],
        env=environment,
        cwd=backend,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_timing_summary_is_byte_identical_across_fresh_processes_and_preserves_means(tmp_path):
    source = tmp_path / "premium_timing.parquet"
    expected = _adversarial_timing_archive(source)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    outputs = [tmp_path / f"summary-{seed}.parquet" for seed in (17, 1729, 2718)]
    for seed, output in zip((17, 1729, 2718), outputs, strict=True):
        _fresh_process_summary(source, output, seed)

    assert outputs[0].read_bytes() == outputs[1].read_bytes() == outputs[2].read_bytes()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    summary = pl.read_parquet(outputs[0])
    assert summary.equals(summary.sort(GROUP_KEYS))
    assert summary.height == len(expected)
    for row in summary.iter_rows(named=True):
        key = tuple(row[column] for column in GROUP_KEYS)
        values = expected[key]
        observed = [value for value in values if value is not None]
        assert row["experiments"] == len(values)
        if not observed:
            assert row["mean_return"] is None
        else:
            # Independent scalar reference: fsum does not use Polars batch means.
            assert row["mean_return"] == pytest.approx(
                math.fsum(observed) / len(observed), rel=1e-12, abs=2e-17
            )
    tiny = summary.filter(
        (pl.col("panel") == "primary")
        & (pl.col("quality") == "primary")
        & (pl.col("screen") == "unscreened")
        & (pl.col("policy") == "maximum_apr")
        & (pl.col("side") == "call")
        & (pl.col("mark_hour_et") == 9)
    )
    assert tiny["mean_return"].to_list() == [2**-60]


def test_empty_timing_summary_preserves_its_schema(tmp_path):
    source = tmp_path / "empty.parquet"
    pq.write_table(pa.Table.from_pylist([], schema=TIMING_SCHEMA), source)
    result = _timing_summary(source)
    assert result.is_empty()
    assert result.columns == [*GROUP_KEYS, "experiments", "mean_return"]
    assert result.schema["experiments"] == pl.UInt32
    assert result.schema["mean_return"] == pl.Float64


def _summary_archives(forecasts: Path, outcomes: Path):
    """Real session dates, distinct availability cohorts, duplicates and null scores."""
    calendar = SessionCalendar()
    forecast_rows, outcome_rows = [], []
    forecast_units, strategy_units = {}, {}
    calibration_reference, loss_reference, pnl_reference = [], [], []
    tiny = (0.0, 2**-55, -2**-55, 2**-53)
    money = (1000.0, 1e-13, -1000.0, 12.25, -12.25, 0.125)
    returns = (0.1 + 2**-55, -0.1, 2**-57, -2**-57, 1e-8, -1e-8)
    for panel, (start, end) in PANELS.items():
        sessions = calendar.sessions(start, end)
        boundary = holdout_boundary(start, end, calendar)
        schedule = sessions[::2]
        holdout_origins = [day for day in schedule if day >= boundary]
        origins = (*schedule[:26], *holdout_origins[-26:])
        for origin_index, origin in enumerate(origins):
            for contract_index in range(320):
                horizon = (1, 4, 20)[contract_index % 3]
                expiry_index = sessions.index(origin) + horizon
                if expiry_index >= len(sessions):
                    continue
                side = "call" if contract_index % 2 == 0 else "put"
                eligible = contract_index % 19 != 0
                challenger = "challenger_a" if contract_index < 160 else "challenger_b"
                for ticker_index, ticker in enumerate(("CIFR", "IREN")):
                    baseline = (0.1 if contract_index < 160 else 0.9) + tiny[
                        (contract_index + origin_index + ticker_index) % len(tiny)
                    ]
                    candidate = baseline + (0.05 if contract_index < 160 else -0.1)
                    common = {
                        "panel": panel,
                        "ticker": ticker,
                        "origin": origin,
                        "expiry_session": sessions[expiry_index],
                        "horizon": horizon,
                        "contract": f"contract-{contract_index}",
                        "side": side,
                        "primary_eligible": eligible,
                        "prediction": 0.15 + tiny[contract_index % len(tiny)],
                        "itm": contract_index % 7 < 3,
                        "atm": False,
                        "loss_probability": 0.25 + tiny[origin_index % len(tiny)],
                        "realized_loss": contract_index % 5 < 2,
                    }
                    for model, brier in (("lognormal_ewma", baseline), (challenger, candidate)):
                        expected_pnl = money[(contract_index + origin_index) % len(money)]
                        realized_pnl = (
                            None if contract_index % 23 == 0 else expected_pnl + 0.125
                        )
                        row = {
                            **common,
                            "model": model,
                            "brier": brier,
                            "log_loss": 1.0 + brier,
                            "crps": 4.0 + brier,
                            "normalized_crps": brier / 100,
                            "pinball": brier / 10,
                            "expected_pnl": expected_pnl,
                            "realized_pnl": realized_pnl,
                            "quantile05": -25.25,
                            "atom": brier / 2,
                            "quantile_atom": brier / 3,
                            "breach_strict": False,
                            "breach_inclusive": False,
                        }
                        copies = 2 if contract_index % 29 == 0 else 1
                        forecast_rows.extend([row] * copies)
                        if panel == "primary" and model == "challenger_a" and side == "call" \
                                and eligible:
                            calibration_reference.extend(
                                [(row["prediction"], row["itm"])] * copies
                            )
                            loss_reference.extend(
                                [(row["loss_probability"], row["realized_loss"])] * copies
                            )
                            if horizon == 1 and realized_pnl is not None:
                                pnl_reference.extend([(expected_pnl, realized_pnl)] * copies)
                    if panel == "primary" and eligible and horizon == 1 and origin >= boundary:
                        forecast_units.setdefault((challenger, ticker, origin), []).append(
                            (candidate, baseline)
                        )
                    for policy in ("near_atm", "maximum_apr"):
                        excess = returns[contract_index % len(returns)] + ticker_index * 1e-7
                        outcome = {
                            **common,
                            "policy": policy,
                            "screen": "unscreened",
                            "quality": "primary",
                            "haircut": 0.0,
                            "fee": 0.0,
                            "stock_slippage_bps": 0,
                            "status": "scored",
                            "excess_return": excess,
                        }
                        outcome_rows.append(outcome)
                        if panel == "primary" and policy == "near_atm" and side == "call" \
                                and horizon == 1 and origin >= boundary:
                            strategy_units.setdefault((ticker, origin), []).append(excess)
    assert len(forecast_rows) > 100_000
    assert len(outcome_rows) > 100_000
    pq.write_table(pa.Table.from_pylist(forecast_rows, schema=FORECAST_SCHEMA), forecasts,
                   compression="zstd", row_group_size=4093)
    pq.write_table(pa.Table.from_pylist(outcome_rows, schema=OUTCOME_SCHEMA), outcomes,
                   compression="zstd", row_group_size=4093)
    return forecast_units, strategy_units, calibration_reference, loss_reference, pnl_reference


def _fresh_process_statistics(forecasts: Path, outcomes: Path, target: Path, seed: int):
    backend = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(POLARS_MAX_THREADS="1", PYTHONHASHSEED=str(seed))
    environment["PYTHONPATH"] = str(backend / "src")
    code = """
import json
import sys
from pathlib import Path
from stocksweeper.research.historical_options import statistics
forecasts, outcomes, target = map(Path, sys.argv[1:])
result = {name: getattr(statistics, name)(forecasts) for name in (
    'forecast_inference', 'calibration_bins', 'loss_calibration_bins', 'forecast_descriptives')}
result['strategy_inference'] = statistics.strategy_inference(outcomes)
target.write_text(json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False))
"""
    subprocess.run(
        [sys.executable, "-c", code, str(forecasts), str(outcomes), str(target)],
        env=environment,
        cwd=backend,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_statistical_reports_are_identical_across_fresh_processes_with_matched_support(tmp_path):
    forecasts, outcomes = tmp_path / "forecast_cells.parquet", tmp_path / "outcomes.parquet"
    forecast_units, strategy_units, calibration, losses, pnl = _summary_archives(
        forecasts, outcomes
    )
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (forecasts, outcomes)]
    outputs = [tmp_path / f"statistics-{seed}.json" for seed in (17, 1729, 2718)]
    for seed, output in zip((17, 1729, 2718), outputs, strict=True):
        _fresh_process_statistics(forecasts, outcomes, output, seed)
    assert outputs[0].read_bytes() == outputs[1].read_bytes() == outputs[2].read_bytes()
    assert before == [hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in (forecasts, outcomes)]
    result = json.loads(outputs[0].read_text())
    for model in ("challenger_a", "challenger_b"):
        row = next(row for row in result["forecast_inference"]
                   if row["panel"] == "primary" and row["band"] == "1"
                   and row["period"] == "holdout" and row["metric"] == "brier"
                   and row["model"] == model)
        units = [pairs for key, pairs in forecast_units.items() if key[0] == model]
        dates = {key[2] for key in forecast_units if key[0] == model}
        assert row["paired_dates"] == len(dates) >= 20
        assert row["paired_units"] == len(units) == len(dates) * 2
        assert row["matched_contract_cells"] == sum(map(len, units))
        for name, index in (("candidate", 0), ("baseline", 1)):
            reference = math.fsum(math.fsum(pair[index] for pair in unit) / len(unit)
                                  for unit in units) / len(units)
            assert row[name] == pytest.approx(reference, abs=1e-14)
        assert row["multiplicity"] == "seven_comparison_familywise"
        assert row["status"] == "estimable"
    for name, reference, bin_number in (("calibration_bins", calibration, 1),
                                        ("loss_calibration_bins", losses, 2)):
        row = next(row for row in result[name] if row["panel"] == "primary"
                   and row["model"] == "challenger_a" and row["side"] == "call"
                   and row["primary_eligible"] and row["bin"] == bin_number)
        assert row["contract_cells"] == len(reference)
        assert row["forecast_mean"] == pytest.approx(
            math.fsum(pair[0] for pair in reference) / len(reference), abs=1e-14
        )
        assert row["observed_rate"] == pytest.approx(
            math.fsum(pair[1] for pair in reference) / len(reference), abs=1e-14
        )
    row = next(row for row in result["forecast_descriptives"] if row["panel"] == "primary"
               and row["model"] == "challenger_a" and row["side"] == "call"
               and row["quality"] == "primary" and row["band"] == "1")
    assert row["pnl_paired_cells"] == len(pnl)
    assert row["paired_expected_pnl_mean"] == pytest.approx(
        math.fsum(pair[0] for pair in pnl) / len(pnl), abs=1e-12
    )
    assert row["paired_pnl_bias"] == pytest.approx(-0.125)
    row = next(row for row in result["strategy_inference"] if row["panel"] == "primary"
               and row["band"] == "1" and row["period"] == "holdout"
               and row["policy"] == "near_atm" and row["side"] == "call")
    assert row["paired_units"] == len(strategy_units)
    assert row["paired_dates"] == len({key[1] for key in strategy_units}) >= 20
    assert row["paired_delta"] == pytest.approx(
        math.fsum(math.fsum(values) / len(values) for values in strategy_units.values())
        / len(strategy_units), abs=1e-14
    )
    assert row["multiplicity"] == "exploratory"
