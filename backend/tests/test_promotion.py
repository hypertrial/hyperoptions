"""Prospective promotion refuses weak evidence and routes live forecasts safely."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from math import exp
from threading import Event

import polars as pl
import pytest

from stocksweeper.forecast import promotion
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.physical_contest import STUDENT_VERSION, ShadowForecast
from stocksweeper.forecast.predictive import (
    PredictiveForecaster,
    SelectionEvidence,
)
from stocksweeper.forecast.promotion import PromotionRegistry

_DECLARED = datetime(2026, 9, 27, 10, tzinfo=UTC)
_HOLDOUT = date(2026, 10, 1)
_PROMOTED = datetime(2026, 12, 1, 10, tzinfo=UTC)


def _report(*, brier: float = -0.02, provenance: str = "as_issued") -> dict:
    return {
        "source": "append-only forecast ledger",
        "provenance": provenance,
        "bands": {
            "1": {
                "candidate": "student_t_ewma",
                "band": "1",
                "period": "holdout",
                "holdout_start": _HOLDOUT.isoformat(),
                "provenance": [provenance],
                "tickers": 20,
                "independent_date_blocks": 20,
                "ticker_origin_horizon_units": 500,
                "contract_forecasts_available": 1000,
                "baseline_contract_forecasts_available": 1000,
                "brier": {"paired_delta": brier, "bootstrap_95": [-0.03, -0.01]},
                "log_loss": {"paired_delta": -0.01, "bootstrap_95": [-0.02, 0.005]},
                "subgroups": {},
                "promotion_gates": {name: True for name in promotion._REQUIRED_GATES},
                "promotion_eligible": True,
            }
        },
    }


def _activate(registry: PromotionRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    registry.predeclare("1", "student_t_ewma", _HOLDOUT, now=_DECLARED)
    monkeypatch.setattr(promotion, "_contest_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(promotion, "_ledger_report", lambda *args, **kwargs: _report())
    registry.promote("1", now=_PROMOTED)


def test_predeclaration_keeps_frozen_behavior_and_weak_or_replay_evidence_cannot_promote(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = PromotionRegistry(tmp_path)
    assert registry.method(1) is None
    with pytest.raises(ValueError, match="predeclared"):
        registry.promote("1", now=_PROMOTED)
    with pytest.raises(ValueError, match="before any origin"):
        registry.predeclare("1", "student_t_ewma", _DECLARED.date(), now=_DECLARED)
    registry.predeclare("1", "student_t_ewma", _HOLDOUT, now=_DECLARED)
    assert registry.method(1) is None
    monkeypatch.setattr(promotion, "_contest_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        promotion, "_ledger_report", lambda *args, **kwargs: _report(provenance="immutable_replay")
    )
    with pytest.raises(ValueError, match="as-issued"):
        registry.promote("1", now=_PROMOTED)
    monkeypatch.setattr(promotion, "_ledger_report", lambda *args, **kwargs: _report(brier=0.01))
    with pytest.raises(ValueError, match="did not pass"):
        registry.promote("1", now=_PROMOTED)
    assert PromotionRegistry(tmp_path).method(1) is None


def test_empty_as_issued_ledger_cannot_promote(tmp_path) -> None:
    registry = PromotionRegistry(tmp_path)
    registry.predeclare("1", "student_t_ewma", _HOLDOUT, now=_DECLARED)
    with pytest.raises(ValueError, match="prospective, as-issued holdout"):
        registry.promote("1", now=_PROMOTED)
    assert registry.method(1) is None


def test_active_champion_is_versioned_and_post_release_regression_rolls_back(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = PromotionRegistry(tmp_path)
    _activate(registry, monkeypatch)
    assert PromotionRegistry(tmp_path).method(1) == "student_t_ewma"
    assert registry.method(3) is None
    assert registry.method(26) == "lognormal_ewma"

    quality = _report(brier=0.02)
    quality["bands"]["1"]["holdout_start"] = "2026-12-02"
    monkeypatch.setattr(
        promotion,
        "_ledger_report",
        lambda _rows, _band, _candidate, start: _report()
        if start == _HOLDOUT
        else quality,
    )
    seen: list[date] = []
    monkeypatch.setattr(
        promotion,
        "_contest_rows",
        lambda _ledger, *, since, as_of, band, methods: seen.append(since) or [],
    )
    assert registry.post_release_check(object(), as_of=datetime(2027, 2, 1, tzinfo=UTC)) == {
        "1": "rolled_back_to_ewma"
    }
    assert seen == [_HOLDOUT]
    assert PromotionRegistry(tmp_path).method(1) == "lognormal_ewma"
    assert registry.post_release_check(object(), as_of=datetime(2027, 2, 1, tzinfo=UTC)) == {}
    registry.predeclare(
        "1",
        "empirical_scaled",
        date(2027, 3, 1),
        now=datetime(2027, 2, 2, tzinfo=UTC),
    )
    assert registry.method(1) == "lognormal_ewma"
    assert registry._read()["past_decisions"][0]["status"] == "rolled_back"


def test_revised_original_holdout_revokes_active_champion(tmp_path, monkeypatch) -> None:
    registry = PromotionRegistry(tmp_path)
    _activate(registry, monkeypatch)
    monkeypatch.setattr(promotion, "_contest_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        promotion,
        "_ledger_report",
        lambda *_args, **_kwargs: _report(brier=0.02),
    )
    assert registry.post_release_check(object(), as_of=datetime(2027, 2, 1, tzinfo=UTC)) == {
        "1": "rolled_back_to_ewma"
    }
    assert PromotionRegistry(tmp_path).method(1) == "lognormal_ewma"
    assert registry._read()["bands"]["1"]["rollback_reason"] == "holdout_evidence_invalidated"


def test_corrupt_promotion_state_fails_closed_to_ewma(tmp_path, monkeypatch) -> None:
    registry = PromotionRegistry(tmp_path)
    _activate(registry, monkeypatch)
    registry.path.write_text("broken")
    assert registry.method(1) == "lognormal_ewma"


def test_concurrent_predeclarations_preserve_both_horizon_bands(tmp_path) -> None:
    first = PromotionRegistry(tmp_path)
    second = PromotionRegistry(tmp_path)
    first_at_write = Event()
    release_first = Event()
    original_write = first._write

    def paused_write(state: dict) -> None:
        first_at_write.set()
        assert release_first.wait(3)
        original_write(state)

    first._write = paused_write
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending_first = executor.submit(
            first.predeclare, "1", "student_t_ewma", _HOLDOUT, now=_DECLARED
        )
        assert first_at_write.wait(2)
        pending_second = executor.submit(
            second.predeclare, "2-5", "empirical_scaled", _HOLDOUT, now=_DECLARED
        )
        try:
            with pytest.raises(FutureTimeout):
                pending_second.result(timeout=0.1)
        finally:
            release_first.set()
        pending_first.result(timeout=3)
        pending_second.result(timeout=3)

    assert set(PromotionRegistry(tmp_path)._read()["bands"]) == {"1", "2-5"}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("brier", "bootstrap_95"), [-0.02, 0.001]),
        (("log_loss", "paired_delta"), 0.001),
        (("log_loss", "bootstrap_95"), [-0.02, 0.011]),
        (("contract_forecasts_available",), 999),
    ],
)
def test_post_release_gate_requires_promotion_quality(path, value) -> None:
    result = deepcopy(_report()["bands"]["1"])
    assert PromotionRegistry._quality_outcome(result) == "passing"
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert PromotionRegistry._quality_outcome(result) == "rolled_back_to_ewma"


class _Prices:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame

    def fetch(self, ticker, start, end):
        frame = self.frame.filter(pl.col("ts") <= end)
        return frame if start is None else frame.filter(pl.col("ts") >= start)


def _bars() -> pl.DataFrame:
    sessions = SessionCalendar().sessions(date(2026, 1, 1), date(2026, 9, 24))[-100:]
    closes = [100 * exp(0.005 * index + (0.002 if index % 2 else -0.002)) for index in range(100)]
    return pl.DataFrame(
        {
            "ts": sessions,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000.0] * 100,
            "dividends": [0.0] * 100,
            "stock_splits": [0.0] * 100,
        }
    )


def test_promoted_band_overrides_empirical_and_candidate_failure_uses_ewma(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stocksweeper.forecast.physical_contest import PhysicalShadowForecaster

    forecaster = PredictiveForecaster(tmp_path, _Prices(_bars()))
    forecaster.prepare("AAPL", date(2026, 9, 24))
    monkeypatch.setattr(
        "stocksweeper.forecast.predictive._empirical_evidence",
        lambda _samples: (SelectionEvidence(), (0.0, 0.01)),
    )
    when = datetime(2026, 9, 24, 23, tzinfo=UTC)
    expiry = date(2026, 9, 25)
    assert forecaster.forecast("AAPL", when, expiry).method == "empirical_scaled"
    _activate(PromotionRegistry(tmp_path), monkeypatch)
    pending = forecaster.forecast("AAPL", when, expiry)
    assert pending.method == "lognormal_ewma"
    assert pending.selection.rejection_reason == "band_champion_not_prepared"
    forecaster.prepare("AAPL", date(2026, 9, 24))

    def candidates(self, current, method, *, clean):
        assert self.forecaster._enable_promotions is False
        assert method == "student_t_ewma"
        assert clean is not None
        candidate = replace(current, method=method, model_version=STUDENT_VERSION)
        return ShadowForecast(candidate, None, 0, 0)

    monkeypatch.setattr(PhysicalShadowForecaster, "cached_candidate", candidates)
    monkeypatch.setattr(
        PhysicalShadowForecaster,
        "forecast_candidates",
        lambda *args, **kwargs: pytest.fail("live lookup must not fit all challengers"),
    )
    selected = forecaster.forecast("AAPL", when, expiry)
    assert selected.method == "student_t_ewma"
    assert selected.model_version == STUDENT_VERSION
    assert selected.probability("call", selected.spot) is not None

    monkeypatch.setattr(
        PhysicalShadowForecaster,
        "cached_candidate",
        lambda *args, **kwargs: ShadowForecast(None, "fit_failed", 0, 0),
    )
    fallback = forecaster.forecast("AAPL", when, expiry)
    assert fallback.method == "lognormal_ewma"
    assert fallback.selection.rejection_reason == "band_champion_unavailable"
    assert forecaster.forecast("AAPL", when, expiry + timedelta(days=39)).method == (
        "lognormal_ewma"
    )
