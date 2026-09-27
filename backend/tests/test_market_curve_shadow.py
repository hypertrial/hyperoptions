import json
from datetime import UTC, date, datetime
from decimal import Decimal
from math import erf, exp, log, sqrt
from pathlib import Path

import numpy as np

from scripts.evaluate_market_curve import report
from options_api.market_curve_shadow import calculate_curve_shadow
from options_api.market_odds import _years_to_close, calculate_market_odds
from options_api.models import OptionQuote
from stocksweeper.storage.db import connect


NOW = datetime(2026, 9, 11, 14, tzinfo=UTC)
EXPIRY = date(2026, 11, 20)


def _quotes(strikes: range, expiry: date = EXPIRY) -> list[OptionQuote]:
    years = _years_to_close(expiry, NOW)

    def cdf(x: float) -> float:
        return (1 + erf(x / sqrt(2))) / 2

    rows = []
    for strike in strikes:
        d1 = (log(100 / strike) + (0.04 + 0.25**2 / 2) * years) / (0.25 * sqrt(years))
        mid = 100 * cdf(d1) - strike * exp(-0.04 * years) * cdf(d1 - 0.25 * sqrt(years))
        rows.append(
            OptionQuote(
                ticker="TEST",
                root="TEST",
                expiration=expiry.isoformat(),
                strike=Decimal(strike),
                call_bid=Decimal(str(mid - 0.02)),
                call_ask=Decimal(str(mid + 0.02)),
                call_open_interest=100,
                put_bid=Decimal("1"),
                put_ask=Decimal("1.1"),
                put_open_interest=100,
            )
        )
    return rows


def test_shadow_curve_fits_dense_consistent_strip_without_publishing_endpoints() -> None:
    result = calculate_curve_shadow(
        _quotes(range(93, 108)),
        Decimal(100),
        lambda _: 0.04,
        {EXPIRY.isoformat()},
        NOW,
    )
    estimate = result.odds[(EXPIRY.isoformat(), Decimal(100))]
    assert estimate.call_itm_probability is not None, result.rejection_reasons
    assert 0.4 < estimate.call_itm_probability < 0.6
    assert estimate.bounds is not None
    assert estimate.bounds[0] <= estimate.call_itm_probability <= estimate.bounds[1]
    assert (EXPIRY.isoformat(), Decimal(93)) not in result.odds
    assert result.held_out_count > 0


def test_shadow_curve_rejects_sparse_or_inconsistent_strip() -> None:
    sparse = calculate_curve_shadow(
        _quotes(range(99, 102)),
        Decimal(100),
        lambda _: 0.04,
        {EXPIRY.isoformat()},
        NOW,
    )
    assert sparse.odds.get((EXPIRY.isoformat(), Decimal(100)), None) is None or (
        sparse.odds[(EXPIRY.isoformat(), Decimal(100))].call_itm_probability is None
    )
    rows = _quotes(range(93, 108))
    rows[7] = rows[7].model_copy(update={"call_bid": Decimal("50"), "call_ask": Decimal("50.04")})
    contradictory = calculate_curve_shadow(
        rows,
        Decimal(100),
        lambda _: 0.04,
        {EXPIRY.isoformat()},
        NOW,
    )
    assert not any(x.call_itm_probability is not None for x in contradictory.odds.values())


def test_held_out_comparison_uses_benchmark_fit_cohort(
    monkeypatch,
) -> None:
    expiries = (date(2026, 10, 16), EXPIRY, date(2027, 1, 8), date(2027, 5, 19))
    rows = [row for expiry in expiries for row in _quotes(range(93, 108), expiry)]
    allowed = {expiry.isoformat() for expiry in expiries}
    benchmark = calculate_market_odds(rows, Decimal(100), lambda _: 0.04, allowed, NOW)
    held = {
        key for key, estimate in benchmark.items() if estimate.held_out_vanilla_price is not None
    }
    assert len(held) == len(expiries)

    fitted_strips: list[set[tuple[str, Decimal]]] = []

    def fast_curve_fit(quotes, **_kwargs):
        fitted_strips.append({(quote.expiration, quote.strike) for quote in quotes})
        return np.asarray([quote.mid for quote in quotes])

    monkeypatch.setattr("options_api.market_curve_shadow._fit", fast_curve_fit)
    result = calculate_curve_shadow(rows, Decimal(100), lambda _: 0.04, allowed, NOW, benchmark)
    assert result.held_out_cohort == "benchmark_as_fitted"
    assert result.held_out_count == len(held)
    assert result.benchmark_held_out_predicted == len(held)
    assert result.shadow_held_out_predicted == result.paired_held_out_count == len(held)
    for expiry in allowed:
        excluded = {key for key in held if key[0] == expiry}
        assert any(
            {key for key in strip if key[0] == expiry}
            == {(expiry, Decimal(strike)) for strike in range(93, 108)} - excluded
            for strip in fitted_strips
        )
    unavailable = calculate_curve_shadow(rows, Decimal(100), lambda _: 0.04, allowed, NOW, {})
    assert unavailable.held_out_cohort == "benchmark_sampler_unavailable"
    assert unavailable.held_out_count == len(held)
    assert unavailable.benchmark_held_out_predicted == unavailable.paired_held_out_count == 0


def test_one_tick_stability_checks_both_sides_of_equal_spaced_bracket(
    monkeypatch,
) -> None:
    observed: set[tuple[int, float]] = set()

    def fit_midpoints(quotes, *, shift_index=None, shift=0, **_kwargs):
        if shift_index is not None:
            observed.add((shift_index, shift))
        return np.asarray([quote.mid for quote in quotes])

    monkeypatch.setattr("options_api.market_curve_shadow._fit", fit_midpoints)
    result = calculate_curve_shadow(
        _quotes(range(93, 108)),
        Decimal(100),
        lambda _: 0.04,
        {EXPIRY.isoformat()},
        NOW,
    )
    assert result.odds[(EXPIRY.isoformat(), Decimal(100))].call_itm_probability is not None
    assert {
        (neighbor, direction * 0.01) for neighbor in (6, 7, 8) for direction in (-1, 1)
    } <= observed


def test_market_curve_report_compares_only_matched_v2_cohorts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STOCKSWEEPER_DATA_DIR", str(tmp_path))
    record = {
        "held_out_cohort": "benchmark_as_fitted",
        "held_out_comparison_ready": True,
        "held_out_count": 4,
        "held_out_inside": 3,
        "shadow_held_out_predicted": 4,
        "benchmark_held_out_inside": 2,
        "benchmark_held_out_predicted": 4,
        "paired_held_out_count": 4,
        "paired_shadow_inside": 3,
        "paired_benchmark_inside": 2,
        "elapsed_ms": 10,
        "benchmark_ms": 20,
        "live_refresh_ms": 30,
        "by_expiry_moneyness": {
            EXPIRY.isoformat(): {
                "near_atm": {
                    "contracts": 10,
                    "benchmark_available": 8,
                    "shadow_available": 7,
                }
            },
        },
    }
    with connect(tmp_path / "results.duckdb") as connection:
        for version in ("v1", "v2"):
            connection.execute(
                """INSERT INTO market_curve_shadow_runs
                   (ticker, chain_fetched_at, source, session_date, model_version, report_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    "TEST",
                    NOW,
                    "nasdaq",
                    NOW.date(),
                    f"constrained-call-curve-shadow-{version}",
                    json.dumps(record),
                ],
            )
    summary = report()
    assert summary["snapshots"] == summary["comparable_snapshots"] == 1
    assert summary["replacement_review_eligible"] is True
    paired = summary["held_out_quote_interval_fit"]["paired"]
    assert paired["predicted"] == 4
    assert paired["shadow_inside_fraction"] == 0.75
    assert paired["benchmark_inside_fraction"] == 0.5
    assert summary["coverage"]["moneyness:near_atm"]["shadow_fraction"] == 0.7
