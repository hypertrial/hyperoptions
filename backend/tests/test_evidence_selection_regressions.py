"""Independence and provenance boundaries for displayed model comparisons."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from options_api.contract_identity import make_watch_key
from options_api.intraday_capture import capture_intraday_window
from options_api.predictive_watch import PredictiveWatchOdds
from options_api.watchlist import OutcomeView, WatchItem, get_watchlist
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.calibration import build_calibration
from stocksweeper.forecast.evidence_reports import build_model_evidence
from stocksweeper.forecast.intraday_evidence import evaluate as evaluate_intraday
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.forecast.physical_evaluation import ContestRow, evaluate_band
from stocksweeper.forecast.predictive import BASELINE_VERSION, PredictiveForecaster

from .test_intraday_capture import _fixture, _future_session
from .test_intraday_prospective_evaluation import _rows
from .test_predictive_watch import _CalibrationLedger, _calibration_rows
from .test_promotion import _Prices, _activate, _bars


def test_forced_baseline_and_watch_ignore_prepared_promoted_champion(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stocksweeper.forecast.physical_contest import STUDENT_VERSION, ShadowForecast
    from stocksweeper.forecast.physical_contest import PhysicalShadowForecaster
    from stocksweeper.forecast.promotion import PromotionRegistry

    completed = date(2026, 9, 24)
    now = datetime(2026, 9, 24, 23, tzinfo=UTC)
    expiry = date(2026, 9, 25)
    forecaster = PredictiveForecaster(tmp_path, _Prices(_bars()))
    forecaster.prepare("AAPL", completed)
    _activate(PromotionRegistry(tmp_path), monkeypatch)

    def promoted(_shadow, frozen, _method, *, clean):
        assert clean is not None
        return ShadowForecast(
            replace(frozen, method="student_t_ewma", model_version=STUDENT_VERSION),
            None, 0, 0,
        )

    monkeypatch.setattr(PhysicalShadowForecaster, "cached_candidate", promoted)
    forecaster.prepare("AAPL", completed)
    assert forecaster.forecast("AAPL", now, expiry).method == "student_t_ewma"
    baseline = forecaster.forecast("AAPL", now, expiry, force_baseline=True)
    assert (baseline.method, baseline.model_version) == ("lognormal_ewma", BASELINE_VERSION)
    assert forecaster.forecast_baseline("AAPL", now, expiry) == baseline

    watch = PredictiveWatchOdds(tmp_path, lambda: now, forecaster, refresh_enabled=False)
    view, issued = watch.lookup("AAPL", "call", expiry, Decimal("100"))
    assert issued == baseline
    assert (view.method, view.evidence_key) == ("lognormal_ewma", "lognormal_ewma:1")


def test_intraday_capture_keeps_ewma_as_its_dated_close_reference(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, forecaster, market, (issue, _), _, now = _fixture(tmp_path, _future_session())
    monkeypatch.setattr(
        forecaster, "_select_promoted",
        lambda frozen: replace(frozen, method="student_t_ewma"),
    )
    assert forecaster.forecast("TEST", now, issue.expiration).method == "student_t_ewma"
    watch = PredictiveWatchOdds(tmp_path, lambda: now, forecaster, refresh_enabled=False)
    baseline = watch.distribution("TEST", issue.expiration, contract_since=issue.input_session)
    assert baseline.method == "lognormal_ewma"
    call = baseline.probability("call", Decimal(issue.strike_exact))
    put = baseline.probability("put", Decimal(issue.strike_exact))
    assert call is not None and put is not None
    issue = replace(
        issue, method=baseline.method, model_version=baseline.model_version,
        itm_probability=call, otm_probability=put, atm_probability=1 - call - put,
    )
    assert ledger.record_batch([(issue, baseline)]) == 1
    assert capture_intraday_window(
        ledger, forecaster, market, [(issue, baseline)], now=now, window="10:00"
    ) == 2
    assert {row["method"] for row in ledger.evaluation_rows()} == {
        "lognormal_ewma", "quote_reanchored_comparator", "intraday_shadow",
    }


def test_same_side_strikes_do_not_inflate_calibration_threshold() -> None:
    rows = _calibration_rows()
    removed = rows.pop(0)
    repeated = dict(rows[1])  # The next call shares a ticker, origin, and horizon.
    strike = Decimal("101")
    repeated["strike_exact"] = "101.000"
    repeated["contract_key"] = make_watch_key(
        repeated["ticker"], repeated["ticker"], "call",
        repeated["expiration"].isoformat(), strike,
    )
    repeated["idempotency_key"] = f"{repeated['contract_key']}:{repeated['input_session']}"
    rows.append(repeated)
    assert removed["side"] == repeated["side"] == "call"
    assert sum(row["side"] == "call" for row in rows) == 500

    evidence = build_calibration(
        _CalibrationLedger(rows), datetime(2025, 5, 1, tzinfo=UTC)
    )
    assert ("test-v1", "completed_close", "1", "near ATM", "call") not in evidence
    assert evidence[("test-v1", "completed_close", "1", "near ATM", "put")][
        "independent_units"
    ] == 500


def test_physical_calibration_bin_counts_independent_unit_once() -> None:
    origin = date(2026, 9, 28)
    expiry = date(2026, 9, 29)
    rows = [
        ContestRow(
            ticker="TEST", origin=origin, expiry_session=expiry, horizon=1,
            strike=strike, side="call", method=method, probability=probability,
            observed_itm=True, provenance="as_issued", input_vintage="same-bars",
        )
        for strike in ("100", "101")
        for method, probability in (("lognormal_ewma", 0.5), ("student_t_ewma", 0.6))
    ]
    report = evaluate_band(rows, "student_t_ewma", "1", bootstrap_samples=100)
    assert report["contract_forecasts_available"] == 2
    assert report["ticker_origin_horizon_units"] == 1
    assert report["calibration_by_side"]["call"][6] == {
        "lower": 0.6, "upper": 0.7, "count": 1,
        "forecast_mean": 0.6, "observed_rate": 1.0,
    }


def test_overlapping_expiries_do_not_create_intraday_significance(tmp_path) -> None:
    calendar = SessionCalendar()
    days = calendar.sessions(date(2026, 9, 28), date(2026, 10, 27))[:20]
    rows = [
        row
        for day in days
        for row in _rows(day=day, expiry=calendar.offset(day, 4))
    ]
    as_of = datetime.combine(max(row["expiration"] for row in rows), datetime.min.time(), UTC)
    as_of += timedelta(hours=23)
    summary = evaluate_intraday(rows, as_of=as_of)["overall"]
    assert summary["scored_contract_windows"] == 20
    assert summary["scored_dates_before_overlap_purge"] == 20
    assert 1 < summary["scored_date_blocks"] < 20

    class Ledger:
        def iter_evaluation_rows(self, **kwargs):
            if "intraday_shadow" not in kwargs["methods"]:
                return iter(())
            lower, upper = kwargs["horizon_range"]
            return (
                row for row in rows
                if row["method"] in kwargs["methods"]
                and lower <= calendar.horizon(row["input_session"], row["expiration"]) <= upper
            )

        def evaluation_skipped_attempts(self, _provenance):
            return 0

        def panel_coverage(self, **_kwargs):
            return {}

    prospective = build_model_evidence(tmp_path, Ledger(), as_of)[
        ("intraday_shadow", "2-5")
    ]["prospective"]
    assert prospective["independent_date_blocks"] == summary["scored_date_blocks"]
    assert prospective["significance"] == "not_estimable"
    assert prospective["brier"]["bootstrap_95"] is None


@pytest.mark.asyncio
async def test_expired_watch_keeps_five_physical_two_market_and_selected_risk() -> None:
    expiry = date(2026, 9, 25)
    report = {"prospective": {"ticker_origin_horizon_units": 0}}
    item = WatchItem(
        id="a" * 32, ticker="TEST", root="TEST", side="call",
        expiration=expiry, strike_exact="100.000", terms_note="standard 100-share terms",
        created_at=datetime(2026, 9, 24, tzinfo=UTC),
        outcome=OutcomeView(status="pending"),
    )
    state = SimpleNamespace(
        clock=lambda: datetime(2026, 9, 28, 21, tzinfo=UTC),
        watchlist=SimpleNamespace(items=lambda *, as_of: [item]),
        market_odds=SimpleNamespace(schedule=lambda _tickers: None),
        predictive_odds=SimpleNamespace(
            schedule=lambda _tickers: None,
            evidence_index=lambda: {"student_t_ewma:1": report},
        ),
        jobs=SimpleNamespace(active=lambda _kind: None),
    )
    response = await get_watchlist(
        SimpleNamespace(app=SimpleNamespace(state=state)), forecast_model="student_t_ewma"
    )
    body = response.model_dump(mode="json")
    saved = body["items"][0]
    assert body["model_evidence"] == {"student_t_ewma:1": report}
    assert [model["method"] for model in saved["physical_models"]] == [
        "lognormal_ewma", "empirical_scaled", "student_t_ewma", "gjr_garch_t",
        "intraday_shadow",
    ]
    assert [model["method"] for model in saved["market_models"]] == [
        "regimelib", "constrained_call_curve",
    ]
    assert all(model["status"] == "unavailable" for model in saved["physical_models"])
    assert all(model["status"] == "unavailable" for model in saved["market_models"])
    assert all(model["model_evidence"] is None for model in saved["physical_models"])
    assert saved["predictive_odds"]["method"] == "student_t_ewma"
    assert saved["hypothetical_risk"]["forecast_method"] == "student_t_ewma"


def test_shared_evidence_key_and_zero_sample_semantics(tmp_path) -> None:
    watch = PredictiveWatchOdds(
        tmp_path, lambda: datetime(2026, 9, 27, tzinfo=UTC), refresh_enabled=False
    )
    evidence = build_model_evidence(
        tmp_path, ForecastLedger(tmp_path), datetime(2026, 9, 27, tzinfo=UTC)
    )
    watch._model_evidence = evidence
    baseline = watch.forecaster.forecast_baseline(
        "TEST", datetime(2026, 9, 27, tzinfo=UTC), date(2026, 9, 29)
    )
    # An unavailable forecast has no usable horizon key; an available model does.
    available = replace(
        baseline, status="available", reason=None, method="lognormal_ewma",
        as_of=date(2026, 9, 25), expiry_session=date(2026, 9, 29), horizon_sessions=2,
        spot=100.0, daily_volatility=0.02, model_version=BASELINE_VERSION,
        terminal_prices=(90.0, 110.0), weights=(0.5, 0.5),
    )
    view = watch.view_for_distribution(available, "call", Decimal("100"))
    key = view.evidence_key
    assert key == "lognormal_ewma:2-5"
    assert view.model_evidence is None
    assert watch.evidence_index()[key] == evidence[("lognormal_ewma", "2-5")]
    assert watch.evidence_index()[key]["prospective"]["ticker_origin_horizon_units"] == 0
    assert watch.evidence_index()["intraday_shadow:2-5"]["prospective"][
        "significance"
    ] == "not_estimable"
