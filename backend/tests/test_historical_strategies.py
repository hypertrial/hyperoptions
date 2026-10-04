"""Causal selection, exact accounting and observed-activity regression fixtures."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest

from options_api.chain import _decision_metrics
from options_api.money import to_pct_tenths
from stocksweeper.research.historical_options.liquidity import run_liquidity
from stocksweeper.research.historical_options.strategies import (
    displayed_apr,
    expiry_accounting,
    run_strategies,
    select_contract,
)

ORIGIN = date(2025, 7, 2)
ENTRY = date(2025, 7, 3)  # Early close; July 4 expiration maps to this session.
EXPIRY = date(2025, 7, 4)


def _candidate(contract="a", strike=95.0, mark=6.0, volume=100.0, transactions=10.0):
    return dict(
        contract=contract,
        strike=strike,
        mark=mark,
        volume=volume,
        transactions=transactions,
        expiry=EXPIRY,
        valid=True,
    )


def _fixture(*, missing_opening=False, reconcile=False, dividend=0.0, split=0.0):
    contracts = [
        ("c95", "call", 95.0, 6.0),
        ("c99", "call", 99.0, 2.0),
        ("c105", "call", 105.0, 1.0),
        ("p95", "put", 95.0, 1.0),
        ("p99", "put", 99.0, 1.0),
        ("p90", "put", 90.0, 1.0),
    ]
    stocks = pl.DataFrame(
        {
            "ticker": ["CIFR"] * 3,
            "ts": [date(2025, 7, 1), ORIGIN, ENTRY],
            "open": [100.0, 100.0, 100.12345],
            "high": [101.0, 101.0, 105.0],
            "low": [99.0, 99.0, 99.0],
            "close": [100.0, 100.0, 104.0],
            "volume": [1000.0] * 3,
            "dividends": [0.0, 0.0, dividend],
            "stock_splits": [0.0, 0.0, split],
        }
    )
    days, hours = [], []
    for contract, side, strike, mark in contracts:
        for session in (ORIGIN, ENTRY):
            start = datetime.combine(session, datetime.min.time(), UTC) + timedelta(hours=13)
            opening = None if missing_opening and contract == "c95" and session == ENTRY else 1.2345
            days.append(
                dict(
                    ticker="CIFR",
                    contract=contract,
                    session=session,
                    expiry=EXPIRY,
                    expiry_session=ENTRY,
                    side=side,
                    strike=strike,
                    mark=mark,
                    volume=100.0,
                    transactions=10.0,
                    opening_open=opening,
                    opening_start=start if opening else None,
                    opening_end=start + timedelta(hours=1) if opening else None,
                    regular_hours=2,
                    reconciliation_failed=reconcile,
                    valid=True,
                    reason=None,
                    adjusted=True,
                )
            )
            for hour in (9, 10, 13, 15):
                regular = hour in (9, 10) if session == ENTRY else True
                if hour == 9 and opening is None:
                    continue
                hstart = start + timedelta(hours=hour - 9)
                hours.append(
                    dict(
                        ticker="CIFR",
                        contract=contract,
                        session=session,
                        expiry=EXPIRY,
                        expiry_session=ENTRY,
                        side=side,
                        strike=strike,
                        hour=hour,
                        open=1.2345 + (hour - 9) * 0.1,
                        high=2.0,
                        low=1.0,
                        close=mark,
                        volume=10.0,
                        transactions=1.0,
                        start=hstart,
                        end=hstart + timedelta(hours=1),
                        regular_session=regular,
                        valid=True,
                        reason=None,
                    )
                )
    return pl.DataFrame(days), stocks, pl.DataFrame(hours)


def test_apr_uses_calendar_dte_and_native_rounding_with_negative_values():
    for side, spot, strike, premium, dte in (
        ("call", "100", "95", "1", 2),
        ("put", "100", "95", "0.012375", 3),
        ("call", "12.375", "12", "0.05125", 7),
    ):
        _, apr, _ = _decision_metrics(side, Decimal(spot), Decimal(strike), Decimal(premium), dte)
        assert displayed_apr(side, spot, strike, premium, dte) == to_pct_tenths(apr)
    assert displayed_apr("call", 100, 95, 1, 2) < 0
    assert displayed_apr("call", 100, 95, 100, 2) is None


def test_policy_ties_and_liquidity_screen_are_decision_only():
    candidates = [
        _candidate("z", 95.0, 1.0, 10.0, 4.0),
        _candidate("b", 95.0, 1.0, 20.0, 5.0),
        _candidate("a", 95.0, 1.0, 20.0, 5.0),
    ]
    chosen = select_contract(candidates, 100, "call", "maximum_apr", ORIGIN)
    assert chosen["contract"] == "a"
    assert chosen["mark"] == 1.0  # Negative APR is eligible.
    assert (
        select_contract(
            candidates[:1], 100, "call", "maximum_apr", ORIGIN, screen="previous_activity_5tx_20vol"
        )
        is None
    )
    with pytest.raises(ValueError, match="unknown liquidity"):
        select_contract(candidates, 100, "call", "maximum_apr", ORIGIN, screen="future_volume")


def test_fractional_money_breakeven_fees_and_capital_benchmark_parity():
    result = expiry_accounting("call", "12.375", "12.125", "0.005125", "12.125")
    assert result["pnl"] == Decimal("0.512500")
    assert result["capital"] == Decimal("1212.500")
    assert result["net_outlay"] == Decimal("1211.987500")
    breakeven = expiry_accounting("call", 12, "12.125", "0.125", 12)
    assert breakeven["pnl"] == 0
    fees = expiry_accounting("put", 12, 13, "0.00125", 13, fee="0.65")
    assert fees["net_premium"] == Decimal("-0.52500")
    assert fees["pnl"] == Decimal("-0.52500")
    assert fees["benchmark_pnl"] == 0
    assert fees["capital"] == 1200
    slipped = expiry_accounting("call", 13, 12, 1, 14, stock_slippage_bps=25)
    assert slipped["stock_entry"] == Decimal("12.0300")
    assert slipped["capital"] == Decimal("1203.0000")
    assert slipped["benchmark_pnl"] == Decimal("197.0000")
    assert slipped["upside_forgone"] == 100
    assert slipped["return"] - slipped["benchmark_return"] == slipped["excess_return"]
    with pytest.raises(ValueError):
        expiry_accounting("put", 12, 13, 0, 13)


def test_missing_opening_produces_no_entry_without_replacement(tmp_path):
    days, stocks, hours = _fixture(missing_opening=True)
    result = run_strategies(days, stocks, hours, tmp_path)
    selected = pl.read_parquet(tmp_path / "selections.parquet").filter(
        (pl.col("policy") == "maximum_apr") & (pl.col("side") == "call")
    )
    assert selected["contract"].unique().to_list() == ["c95"]
    assert selected["status"].unique().to_list() == ["no_entry"]
    outcomes = pl.read_parquet(tmp_path / "outcomes.parquet").filter(
        (pl.col("policy") == "maximum_apr") & (pl.col("side") == "call")
    )
    assert outcomes["return"].null_count() == outcomes.height
    assert result["counts"]["primary_no_entry"] == 8
    assert selected["horizon"].unique().to_list() == [1]
    assert selected["calendar_dte"].unique().to_list() == [2]
    assert selected["entry_session"].to_list() == selected["expiry_session"].to_list()


def test_future_perturbations_leave_decision_selection_unchanged(tmp_path):
    days, stocks, hours = _fixture()
    run_strategies(days, stocks, hours, tmp_path / "a")
    future_days = days.with_columns(
        pl.when(pl.col("session") > ORIGIN)
        .then(999999.0)
        .otherwise(pl.col("volume"))
        .alias("volume"),
        pl.when(pl.col("session") > ORIGIN).then(222.0).otherwise(pl.col("mark")).alias("mark"),
    )
    future_stocks = stocks.with_columns(
        pl.when(pl.col("ts") > ORIGIN).then(900.0).otherwise(pl.col("close")).alias("close")
    )
    future_hours = hours.with_columns(pl.lit(555.0).alias("high"), pl.lit(999.0).alias("volume"))
    run_strategies(future_days, future_stocks, future_hours, tmp_path / "b")
    columns = [
        "selection_id",
        "contract",
        "displayed_apr_pct_tenths",
        "decision_mark",
        "decision_volume",
        "horizon",
        "policy",
        "screen",
        "quality",
    ]
    assert (
        pl.read_parquet(tmp_path / "a/selections.parquet")
        .select(columns)
        .equals(pl.read_parquet(tmp_path / "b/selections.parquet").select(columns))
    )


def test_reconciliation_sensitivity_and_fixed_cost_grids(tmp_path):
    days, stocks, hours = _fixture(reconcile=True)
    run_strategies(days, stocks, hours, tmp_path)
    outcomes = pl.read_parquet(tmp_path / "outcomes.parquet")
    assert outcomes.filter(pl.col("quality") == "primary")["return"].null_count() == (
        outcomes.filter(pl.col("quality") == "primary").height
    )
    included = outcomes.filter(pl.col("quality") == "reconciliation_inclusion")
    assert included["status"].unique().to_list() == ["scored"]
    puts = included.filter(pl.col("side") == "put")
    assert puts["stock_slippage_bps"].unique().to_list() == [0]
    assert puts.height == puts["selection_id"].n_unique() * 8
    calls = included.filter(pl.col("side") == "call")
    assert calls.height == calls["selection_id"].n_unique() * 24
    assert set(calls["haircut"].to_list()) == {0.0, 0.05, 0.10, 0.20}
    assert included["contract"].n_unique() <= 6
    excluded = pl.read_parquet(tmp_path / "strategy_exclusions.parquet")
    assert "reconciliation_failure" in excluded["reason"].to_list()


@pytest.mark.parametrize("action", ["dividend", "split"])
def test_action_exclusions_apply_after_selection_and_retain_identity(tmp_path, action):
    days, stocks, hours = _fixture(**{action: 2.0})
    run_strategies(days, stocks, hours, tmp_path)
    selected = pl.read_parquet(tmp_path / "selections.parquet")
    assert selected["status"].unique().to_list() == ["excluded"]
    assert selected.filter((pl.col("side") == "call") & (pl.col("policy") == "maximum_apr"))[
        "contract"
    ].unique().to_list() == ["c95"]


def test_early_close_timing_and_all_archive_rows_audited(tmp_path):
    days, stocks, hours = _fixture()
    run_strategies(days, stocks, hours, tmp_path)
    timing = pl.read_parquet(tmp_path / "premium_timing.parquet")
    assert timing.filter(pl.col("mark_hour_et") == 15)["status"].unique().to_list() == [
        "outside_session"
    ]
    assert timing.filter(pl.col("mark_hour_et") == 10)["mark"].null_count() == 0
    assert timing["stock_entry"].unique().to_list() == [100.12345]
    result = run_liquidity(days, stocks, hours, tmp_path)
    assert result["archive_rows"] == result["accounted_archive_rows"] == hours.height
    assert result["archive"][0]["outside_regular_session_bars"] == 12
    assert result["archive"][0]["single_transaction_bars"] == hours.height
    liquidity = pl.read_parquet(tmp_path / "liquidity.parquet")
    assert liquidity["bars"].sum() == hours.height


def test_unknown_stock_coverage_is_not_moneyness_or_zero_prices(tmp_path):
    days, stocks, hours = _fixture()
    stocks = stocks.filter(pl.col("ts") != ENTRY)
    result = run_liquidity(days, stocks, hours, tmp_path)
    liquidity = pl.read_parquet(tmp_path / "liquidity.parquet")
    assert "unverified_stock" in liquidity["strike_distance_band"].to_list()
    assert result["contract_days"][0]["unverified_stock_days"] == 6


def test_individual_liquidity_screens_have_distinct_predicates():
    high_transactions = [_candidate(volume=19.0, transactions=5.0)]
    high_volume = [_candidate(volume=20.0, transactions=4.0)]
    assert (
        select_contract(
            high_transactions, 100, "call", "maximum_apr", ORIGIN, screen="previous_transactions_5"
        )
        is not None
    )
    assert (
        select_contract(
            high_transactions, 100, "call", "maximum_apr", ORIGIN, screen="previous_volume_20"
        )
        is None
    )
    assert (
        select_contract(
            high_volume, 100, "call", "maximum_apr", ORIGIN, screen="previous_volume_20"
        )
        is not None
    )
    assert (
        select_contract(
            high_volume, 100, "call", "maximum_apr", ORIGIN, screen="previous_transactions_5"
        )
        is None
    )


def test_ranking_haircuts_use_decision_marks_and_report_complete_rank_stability(tmp_path):
    days, stocks, hours = _fixture()
    run_strategies(days, stocks, hours, tmp_path)
    ranks = pl.read_parquet(tmp_path / "ranking_stability.parquet").filter(
        (pl.col("side") == "call")
        & (pl.col("screen") == "unscreened")
        & (pl.col("haircut") == 0.20)
    )
    assert ranks["contract"].to_list() == ["c95"]
    assert ranks["haircut_contract"].to_list() == ["c99"]
    assert ranks["candidate_count"].to_list() == [2]
    assert ranks["matched_rank_count"].to_list() == [2]
    assert ranks["spearman_rank_correlation"].to_list() == [-1.0]


def test_missing_or_nonfinite_action_metadata_is_excluded_without_invention(tmp_path):
    days, stocks, hours = _fixture()
    stocks = stocks.with_columns(
        pl.when(pl.col("ts") == ENTRY)
        .then(float("nan"))
        .otherwise(pl.col("dividends"))
        .alias("dividends")
    )
    run_strategies(days, stocks, hours, tmp_path)
    selections = pl.read_parquet(tmp_path / "selections.parquet")
    assert selections["reason"].unique().to_list() == ["unknown_corporate_action_metadata"]


def test_maturities_beyond_primary_are_labeled_other_eligible(tmp_path):
    days, stocks, hours = _fixture()
    # Shift into the year-end interval; Dec 31 -> Jan 2 are consecutive sessions.
    origin, entry, expiry = date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 2)
    mapping = {date(2025, 7, 1): date(2025, 12, 30), ORIGIN: origin, ENTRY: entry}
    days = days.with_columns(
        pl.col("session").replace_strict(mapping).alias("session"),
        pl.lit(expiry).alias("expiry"),
        pl.lit(entry).alias("expiry_session"),
    )
    stocks = stocks.with_columns(pl.col("ts").replace_strict(mapping).alias("ts"))
    hours = hours.with_columns(
        pl.col("session").replace_strict(mapping).alias("session"),
        pl.lit(expiry).alias("expiry"),
        pl.lit(entry).alias("expiry_session"),
    )
    run_strategies(days, stocks, hours, tmp_path)
    selections = pl.read_parquet(tmp_path / "selections.parquet")
    assert selections["panel"].unique().to_list() == ["other_eligible"]
    assert selections["status"].unique().to_list() == ["scored"]
