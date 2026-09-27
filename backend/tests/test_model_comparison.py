"""Live model selection reads only matching prepared snapshots."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from options_api.market_curve_shadow import CurveShadowResult
from options_api.market_odds import OddsEstimate
from options_api.market_watch import MarketWatchOdds, _Snapshot
from options_api.physical_shadow_capture import PhysicalShadowCapture
from options_api.predictive_watch import PredictiveWatchOdds
from stocksweeper.forecast.physical_contest import ShadowForecast
from stocksweeper.forecast.predictive import PredictiveDistribution


def test_candidate_cache_rejects_revised_input_and_unsupported_horizon(tmp_path) -> None:
    predictive = PredictiveWatchOdds(tmp_path, lambda: datetime.now(UTC), refresh_enabled=False)
    capture = PhysicalShadowCapture(predictive)
    origin = date(2026, 9, 25)
    expiry = date(2026, 10, 2)
    baseline = PredictiveDistribution(
        ticker="IREN", status="available", reason=None,
        method="lognormal_ewma", as_of=origin, expiry_session=expiry,
        horizon_sessions=5, spot=100.0, daily_volatility=0.02,
        model_version="baseline-v1", support=60, data_hash="original",
        terminal_prices=(90.0, 110.0), weights=(0.5, 0.5),
    )
    challenger = replace(baseline, method="student_t_ewma", model_version="student-v1")
    capture._live[("IREN", origin, expiry, origin, "original")] = (
        {"student_t_ewma": ShadowForecast(challenger, None, 0, 0)}, None,
    )

    assert capture.candidate(baseline, "student_t_ewma", expiry, origin).distribution == challenger
    revised = replace(baseline, data_hash="revised")
    assert capture.candidate(revised, "student_t_ewma", expiry, origin).reason == (
        "candidate_not_prepared"
    )
    assert capture.candidate(revised, "lognormal_ewma", expiry, origin).distribution == revised
    long_horizon = replace(baseline, horizon_sessions=26)
    assert capture.candidate(long_horizon, "student_t_ewma", expiry, origin).reason == (
        "candidate_horizon_unsupported"
    )


def test_curve_result_is_hidden_after_quote_snapshot_rollover() -> None:
    now = datetime(2026, 9, 25, 18, tzinfo=UTC)
    expiry = "2026-10-02"
    strike = Decimal("100")
    first = _Snapshot(
        now, date(2026, 9, 25), "nasdaq", {}, {},
        valid_contracts={(expiry, strike)},
    )
    odds = MarketWatchOdds.__new__(MarketWatchOdds)
    odds.clock = lambda: now
    odds._valid = lambda _snapshot, _now: True
    odds._cache = {"IREN": first}
    odds._curve_shadow = {}
    odds._curve_results = {
        "IREN": (
            first,
            CurveShadowResult(
                {(expiry, strike): OddsEstimate(0.4, bounds=(0.3, 0.5))},
                1, 1, 5.0, {},
            ),
        )
    }
    available = odds.lookup_curve("IREN", "call", expiry, strike, "IREN")
    assert available.status == "available"
    assert (available.itm_pct_tenths, available.otm_pct_tenths) == (400, 600)

    odds._cache["IREN"] = replace(first)
    stale = odds.lookup_curve("IREN", "call", expiry, strike, "IREN")
    assert stale.status == "pending"
    assert stale.itm_pct_tenths is None


def test_curve_result_preserves_sparse_and_failed_fit_reasons() -> None:
    now = datetime(2026, 9, 25, 18, tzinfo=UTC)
    expiry = "2026-10-02"
    strike = Decimal("100")
    snapshot = _Snapshot(
        now, date(2026, 9, 25), "nasdaq", {}, {},
        valid_contracts={(expiry, strike)},
    )
    odds = MarketWatchOdds.__new__(MarketWatchOdds)
    odds.clock = lambda: now
    odds._valid = lambda _snapshot, _now: True
    odds._cache = {"IREN": snapshot}
    odds._curve_shadow = {}
    odds._curve_results = {
        "IREN": (
            snapshot,
            CurveShadowResult(
                {}, 0, 0, 1.0, {"sparse_or_large_strip": 3},
                expiry_reasons={expiry: "sparse_or_large_strip"},
            ),
        ),
    }
    sparse = odds.lookup_curve("IREN", "call", expiry, strike, "IREN")
    assert sparse.status == "unavailable"
    assert sparse.reason == "sparse_or_large_strip"
    assert sparse.model_evidence["bid_ask_fit"] is None

    odds._curve_results["IREN"] = (
        snapshot, CurveShadowResult({}, 0, 0, 0, {"curve_fit_failed": 1})
    )
    failed = odds.lookup_curve("IREN", "call", expiry, strike, "IREN")
    assert failed.reason == "curve_fit_failed"
