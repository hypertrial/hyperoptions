"""Adversarial forecast tests without network access or live market data."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from math import exp
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import polars as pl
import pytest

from stocksweeper.forecast.calibration import CalibrationResult
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import (
    ForecastPriceStore,
    MAX_PRICE_BARS,
    clean_completed,
    normalize_prices,
    price_hash,
)
from stocksweeper.forecast.models import PeerCandidate
from stocksweeper.forecast.samples import observations
from stocksweeper.forecast.selection import Selection, catalog, select_strategy, strategy_states
from stocksweeper.forecast.service import ForecastService, _audit_tickers
from stocksweeper.data.synthetic import synthetic_ohlcv
from stocksweeper.strategy.generator import generate_strategies


def _bars(end: date, count: int = 700) -> pl.DataFrame:
    sessions = SessionCalendar().sessions(date(2016, 1, 1), end)[-count:]
    prices = [100 + index * 0.03 for index in range(len(sessions))]
    return pl.DataFrame(
        {
            "ts": sessions,
            "open": prices,
            "high": [price + 1 for price in prices],
            "low": [price - 1 for price in prices],
            "close": prices,
            "volume": [1000.0] * len(sessions),
            "dividends": [0.0] * len(sessions),
            "stock_splits": [0.0] * len(sessions),
        }
    )


class NoNetwork:
    calls = 0

    def fetch(self, ticker, start, end):
        self.calls += 1
        raise AssertionError("unexpected price download")

    def sector(self, ticker):
        return "Technology"


def test_no_future_audit_data_is_downloaded_for_historical_forecast(tmp_path):
    provider = NoNetwork()
    service = ForecastService(tmp_path, provider)
    service.initialize()
    snapshot = service.refresh_contract(
        "AAPL",
        "call",
        Decimal("100"),
        date(2024, 6, 28),
        as_of=datetime(2024, 6, 3, 23, tzinfo=UTC),
        candidates=[],
    )
    assert snapshot.status == "unavailable"
    assert snapshot.reason == "audit_window_incomplete"
    assert provider.calls == 0


def test_split_after_watch_creation_withholds_original_strike_probability(tmp_path, monkeypatch):
    service = ForecastService(tmp_path, NoNetwork())
    service.initialize()
    bars = _bars(date(2026, 9, 24)).with_columns(
        pl.when(pl.col("ts") == date(2026, 9, 24))
        .then(pl.lit(2.0))
        .otherwise(pl.col("stock_splits"))
        .alias("stock_splits")
    )
    monkeypatch.setattr(service.prices, "update", lambda *args, **kwargs: bars)
    snapshot = service.refresh_contract(
        "AAPL",
        "call",
        Decimal("100"),
        date(2026, 10, 1),
        as_of=datetime(2026, 9, 24, 23, tzinfo=UTC),
        candidates=[],
        watched_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    assert snapshot.status == "unavailable"
    assert snapshot.reason == "contract_terms_changed"


def test_negative_split_action_withholds_original_strike_probability(tmp_path, monkeypatch):
    service = ForecastService(tmp_path, NoNetwork())
    service.initialize()
    bars = _bars(date(2026, 9, 24), count=720)
    bad_day = bars["ts"][-2]
    bars = bars.with_columns(
        pl.when(pl.col("ts") == bad_day)
        .then(pl.lit(-2.0))
        .otherwise(pl.col("stock_splits"))
        .alias("stock_splits")
    )
    monkeypatch.setattr(service.prices, "update", lambda *args, **kwargs: bars)
    snapshot = service.refresh_contract(
        "AAPL", "call", Decimal("100"), date(2026, 10, 1),
        as_of=datetime(2026, 9, 24, 23, tzinfo=UTC), candidates=[],
        watched_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert snapshot.status == "unavailable"
    assert snapshot.reason == "contract_terms_ambiguous"


def test_missing_action_metadata_fails_closed():
    frame = pd.DataFrame(
        {
            "Date": [date(2026, 9, 24)],
            "Open": [100.0],
            "High": [101.0],
            "Low": [99.0],
            "Close": [100.0],
            "Volume": [1000.0],
            "Dividends": [0.0],
        }
    )
    with pytest.raises(ValueError, match="split action metadata"):
        normalize_prices(frame, "AAPL")


def test_oversized_yahoo_frame_is_rejected_before_conversion(monkeypatch):
    small = pd.DataFrame({
        "Date": [date(2026, 9, 24)],
        "Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.0],
        "Volume": [1000.0], "Dividends": [0.0], "Stock Splits": [0.0],
    })
    assert normalize_prices(small, "AAPL").height == 1

    oversized = pd.DataFrame({"Close": [100.0] * (MAX_PRICE_BARS + 1)})
    monkeypatch.setattr(
        "stocksweeper.forecast.market.pl.from_pandas",
        lambda *_args: pytest.fail("oversized frame was converted"),
    )
    with pytest.raises(ValueError, match="bounded bar limit"):
        normalize_prices(oversized, "AAPL")


def test_clean_prices_exclude_an_unfinished_session():
    bars = _bars(date(2026, 9, 25))
    clean = clean_completed(bars, date(2026, 9, 24), SessionCalendar())
    assert clean["ts"][-1] == date(2026, 9, 24)
    assert clean.height == bars.height - 1


def test_malformed_volume_and_action_rows_are_not_clean_prefix_bars():
    bars = _bars(date(2026, 9, 24))
    bad_day = bars["ts"][10]
    bad_volume = bars.with_columns(
        pl.when(pl.col("ts") == bad_day)
        .then(pl.lit(-1.0))
        .otherwise(pl.col("volume"))
        .alias("volume")
    )
    bad_action = bars.with_columns(
        pl.when(pl.col("ts") == bad_day)
        .then(pl.lit(float("nan")))
        .otherwise(pl.col("dividends"))
        .alias("dividends")
    )
    calendar = SessionCalendar()
    assert clean_completed(bad_volume, bars["ts"][-1], calendar).height == bars.height - 1
    assert clean_completed(bad_action, bars["ts"][-1], calendar).height == bars.height - 1


def test_prospective_next_open_state_matches_later_full_frame():
    bars = synthetic_ohlcv(130, seed=11, ticker="AAPL")
    for strategy in generate_strategies(8, seed=7)[:3]:
        full = strategy_states(bars, strategy)
        for length in (60, 90, 120):
            prefix = strategy_states(bars.head(length), strategy)
            assert prefix == full[:length]


def test_rule_selection_reads_only_its_first_600_clean_bars(tmp_path, monkeypatch):
    strategy = generate_strategies(1, seed=7)[0]
    monkeypatch.setattr("stocksweeper.forecast.selection.catalog", lambda: ((strategy,), "catalog"))
    seen = []

    def evaluate(settings, store, ticker, rules, requests, run_id):
        frame = store.read(ticker)
        seen.append(
            (
                frame.height,
                frame["ts"][-1],
                price_hash(
                    frame.with_columns(
                        pl.lit(0.0).alias("dividends"), pl.lit(0.0).alias("stock_splits")
                    )
                ),
            )
        )
        return SimpleNamespace(
            qualified=[
                {
                    "rejected": False,
                    "val_sharpe": 0.5,
                    "score": 0.4,
                    "strategy_id": strategy.id,
                }
            ]
        )

    monkeypatch.setattr("stocksweeper.pipeline.sweep._evaluate_ticker", evaluate)
    bars = _bars(date(2026, 9, 24))
    first, reason = select_strategy("AAPL", bars, date(2026, 9, 24), tmp_path)
    changed = bars.with_columns(
        pl.when(pl.col("ts") == bars["ts"][-1])
        .then(pl.col("close") + 0.1)
        .otherwise(pl.col("close"))
        .alias("close")
    )
    second, reason2 = select_strategy("AAPL", changed, date(2026, 9, 24), tmp_path)
    assert reason is reason2 is None
    assert first is not None and second is not None
    assert first.strategy.id == second.strategy.id
    assert first.prefix_hash == second.prefix_hash
    assert seen[0] == seen[1]
    assert seen[0][0] == 600


def test_scheduled_maturity_does_not_jump_over_missing_bar():
    sessions = SessionCalendar().sessions(date(2021, 1, 4), date(2021, 5, 31))[:70]
    closes = []
    value = 100.0
    for index in range(len(sessions)):
        value *= exp(0.01 if index % 2 else -0.005)
        closes.append(value)
    frame = pl.DataFrame(
        {
            "ts": sessions,
            "open": closes,
            "high": [price + 1 for price in closes],
            "low": [price - 1 for price in closes],
            "close": closes,
            "volume": [1000.0] * len(sessions),
            "dividends": [0.0] * len(sessions),
            "stock_splits": [0.0] * len(sessions),
        }
    )
    missing = frame.filter(pl.col("ts") != sessions[50])
    rows = observations(
        "AAPL",
        missing,
        ("long",) * missing.height,
        1,
        date(2021, 1, 1),
        date(2021, 12, 31),
        SessionCalendar(),
    )
    assert not any(row.as_of == sessions[49] for row in rows)
    assert not any(row.maturity == sessions[50] for row in rows)
    assert any(row.as_of == sessions[48] for row in rows)


def test_price_correction_replaces_tail_and_changes_data_hash(tmp_path):
    class CorrectedProvider(NoNetwork):
        def fetch(self, ticker, start, end):
            self.calls += 1
            frame = _bars(end)
            if self.calls >= 2:
                frame = frame.with_columns(
                    pl.when(pl.col("ts") == frame["ts"][-10])
                    .then(pl.col("close") + 0.1)
                    .otherwise(pl.col("close"))
                    .alias("close")
                )
            return frame if start is None else frame.filter(pl.col("ts") >= start)

    provider = CorrectedProvider()
    store = ForecastPriceStore(tmp_path, provider)
    first = store.update("AAPL", date(2026, 9, 24))
    revised = store.update("AAPL", date(2026, 9, 24))
    assert provider.calls == 3  # changed overlap causes a full-history rebase
    assert first.height == revised.height == 700
    assert revised["close"][-10] == pytest.approx(first["close"][-10] + 0.1)
    assert price_hash(first) != price_hash(revised)


def test_sparse_overlap_response_cannot_erase_cached_split(tmp_path):
    class SparseTail(NoNetwork):
        def fetch(self, ticker, start, end):
            self.calls += 1
            frame = _bars(end).with_columns(
                pl.when(pl.col("ts") == date(2026, 9, 22))
                .then(pl.lit(2.0))
                .otherwise(pl.col("stock_splits"))
                .alias("stock_splits")
            )
            if start is not None:
                frame = frame.filter((pl.col("ts") >= start) & (pl.col("ts") != date(2026, 9, 22)))
            elif self.calls > 1:
                frame = frame.filter(pl.col("ts") != date(2026, 9, 22))
            return frame

    provider = SparseTail()
    store = ForecastPriceStore(tmp_path, provider)
    initial = store.update("AAPL", date(2026, 9, 24))
    merged = store.update("AAPL", date(2026, 9, 24))
    assert merged.height == initial.height
    assert merged.filter(pl.col("ts") == date(2026, 9, 22))["stock_splits"][0] == 2.0
    service = ForecastService(tmp_path, provider)
    service.initialize()
    snapshot = service.refresh_contract(
        "AAPL",
        "call",
        Decimal("100"),
        date(2026, 10, 1),
        as_of=datetime(2026, 9, 24, 23, tzinfo=UTC),
        candidates=[],
        watched_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    assert snapshot.reason == "contract_terms_changed"
    with pytest.raises(ValueError, match="omitted cached sessions"):
        store.update("AAPL", date(2026, 9, 24), full_refresh=True)
    assert store.read("AAPL").filter(pl.col("ts") == date(2026, 9, 22))["stock_splits"][0] == 2.0


def test_new_split_rebases_entire_cached_history_before_new_watch(tmp_path, monkeypatch):
    class SplitProvider(NoNetwork):
        def fetch(self, ticker, start, end):
            self.calls += 1
            if end == date(2026, 9, 21):
                return _bars(end)
            frame = _bars(end, 703).with_columns(
                *[pl.col(name) * 0.5 for name in ("open", "high", "low", "close")],
                pl.when(pl.col("ts") == date(2026, 9, 22))
                .then(pl.lit(2.0))
                .otherwise(pl.col("stock_splits"))
                .alias("stock_splits"),
            )
            return frame if start is None else frame.filter(pl.col("ts") >= start)

    provider = SplitProvider()
    store = ForecastPriceStore(tmp_path, provider)
    original = store.update("AAPL", date(2026, 9, 21))
    rebased = store.update("AAPL", date(2026, 9, 24))
    assert provider.calls == 3
    assert rebased["ts"][0] == original["ts"][0]
    assert rebased["close"][0] == pytest.approx(original["close"][0] * 0.5)
    assert rebased.filter(pl.col("ts") == date(2026, 9, 22))["stock_splits"][0] == 2.0

    service = ForecastService(tmp_path, provider)
    service.initialize()
    strategy = generate_strategies(1, seed=7)[0]
    selection = Selection(
        strategy=strategy,
        cutoff=rebased["ts"][599],
        prefix_hash=price_hash(rebased.head(600)),
        catalog_hash="catalog",
        validation_sharpe=1.0,
        robustness=1.0,
    )
    monkeypatch.setattr(service, "_selection", lambda *args, **kwargs: (selection, None))
    monkeypatch.setattr(
        "stocksweeper.forecast.service.strategy_states", lambda bars, _: ("flat",) * bars.height
    )
    monkeypatch.setattr(
        "stocksweeper.forecast.service.current_state_and_volatility",
        lambda *args: ("flat", 0.02, 60.0),
    )
    monkeypatch.setattr(service, "_cohort", lambda *args: None)
    snapshot = service.refresh_contract(
        "AAPL",
        "call",
        Decimal("60"),
        date(2026, 10, 1),
        as_of=datetime(2026, 9, 24, 23, tzinfo=UTC),
        candidates=[],
        watched_at=datetime(2026, 9, 23, tzinfo=UTC),
    )
    assert snapshot.reason == "cohort_insufficient"


def test_target_prices_full_refresh_on_month_rollover(tmp_path):
    class RevisedHistory(NoNetwork):
        def __init__(self):
            self.starts = []

        def fetch(self, ticker, start, end):
            self.starts.append(start)
            frame = _bars(end, 700 if end == date(2026, 9, 24) else 710)
            if end == date(2026, 10, 2):
                frame = frame.with_columns(
                    pl.when(pl.col("ts") == original_day)
                    .then(pl.col("close") + 0.1)
                    .otherwise(pl.col("close"))
                    .alias("close")
                )
            return frame if start is None else frame.filter(pl.col("ts") >= start)

    provider = RevisedHistory()
    store = ForecastPriceStore(tmp_path, provider)
    first = store.update("AAPL", date(2026, 9, 24))
    original_day = first["ts"][100]
    revised = store.update("AAPL", date(2026, 10, 2))
    assert provider.starts == [None, None]
    assert revised.filter(pl.col("ts") == original_day)["close"][0] != first["close"][100]
    assert price_hash(revised) != price_hash(first)


def test_failed_cohort_attempt_survives_restart(tmp_path, monkeypatch):
    class OnePeer(NoNetwork):
        def fetch(self, ticker, start, end):
            self.calls += 1
            return _bars(date(2019, 12, 31), 600)

    provider = OnePeer()
    first = ForecastService(tmp_path, provider)
    first.initialize()
    monkeypatch.setattr(first, "_selection", lambda *args, **kwargs: (None, "failed"))
    candidates = [PeerCandidate(ticker="AAPL", sector="Technology")]
    updates = []
    assert first._cohort(date(2026, 9, 24), candidates, lambda p, m: updates.append((p, m))) is None
    assert provider.calls == 1
    assert any("Qualifying Nasdaq peers" in message for _, message in updates)
    assert all(0 <= fraction <= 1 for fraction, _ in updates)
    restarted = ForecastService(tmp_path, provider)
    restarted.initialize()
    assert restarted._cohort(date(2026, 9, 24), candidates) is None
    assert provider.calls == 1


def test_transient_cohort_outage_can_retry_after_restart(tmp_path, monkeypatch):
    class TransientPeer(NoNetwork):
        def fetch(self, ticker, start, end):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary provider outage")
            return _bars(date(2019, 12, 31), 600)

    provider = TransientPeer()
    strategy = generate_strategies(1, seed=7)[0]
    bars = _bars(date(2019, 12, 31), 600)
    selection = Selection(
        strategy=strategy,
        cutoff=bars["ts"][-1],
        prefix_hash=price_hash(bars),
        catalog_hash="catalog",
        validation_sharpe=1.0,
        robustness=1.0,
    )
    monkeypatch.setattr("stocksweeper.forecast.service.MIN_COHORT", 1)
    first = ForecastService(tmp_path, provider)
    first.initialize()
    monkeypatch.setattr(first, "_selection", lambda *args, **kwargs: (selection, None))
    candidates = [PeerCandidate(ticker="AAPL", sector="Technology")]
    assert first._cohort(date(2026, 9, 24), candidates) is None
    assert first._cohort_reason == "peer_data_missing"
    restarted = ForecastService(tmp_path, provider)
    restarted.initialize()
    monkeypatch.setattr(restarted, "_selection", lambda *args, **kwargs: (selection, None))
    assert restarted._cohort(date(2026, 9, 24), candidates) is None
    assert provider.calls == 1
    cohort = restarted._cohort(date(2026, 9, 24), candidates, force_rebuild=True)
    assert cohort is not None
    assert len(cohort["members"]) == 1
    assert provider.calls == 2


def test_peer_without_nasdaq_sector_never_triggers_metadata_lookup(tmp_path):
    provider = NoNetwork()
    service = ForecastService(tmp_path, provider)
    service.initialize()
    assert service._cohort(date(2026, 9, 24), [PeerCandidate(ticker="AAPL", sector=None)]) is None
    assert service._cohort_reason == "peer_data_missing"
    assert provider.calls == 0


def test_holdout_partition_is_ticker_disjoint_and_balanced_within_sectors():
    members = [
        {"ticker": f"T{index:03}", "sector": "Technology" if index < 40 else "Energy"}
        for index in range(80)
    ]
    audit = _audit_tickers(members)
    assert len(audit) == 20
    assert len(audit & {item["ticker"] for item in members[:40]}) == 10
    assert len(audit & {item["ticker"] for item in members[40:]}) == 10
    assert _audit_tickers(list(reversed(members))) == audit
    minimum = [
        {"ticker": f"S{index:03}", "sector": "A" if index < 35 else "B"} for index in range(67)
    ]
    assert len(_audit_tickers(minimum)) == 17


def test_forecast_service_semantic_code_changes_invalidate_catalog_hash(monkeypatch):
    original = catalog()[1]
    read_bytes = Path.read_bytes

    def revised_bytes(path):
        source = read_bytes(path)
        return (
            source + b"\n# changed forecast semantics\n"
            if path.name == "service.py" and path.parent.name == "forecast"
            else source
        )

    monkeypatch.setattr(Path, "read_bytes", revised_bytes)
    catalog.cache_clear()
    try:
        assert catalog()[1] != original
    finally:
        catalog.cache_clear()


def test_revised_peer_selection_requires_new_audited_cohort_version(tmp_path, monkeypatch):
    service = ForecastService(tmp_path, NoNetwork())
    service.initialize()
    bars = _bars(date(2025, 12, 31))
    strategy = generate_strategies(1, seed=7)[0]
    changed = Selection(
        strategy=strategy,
        cutoff=bars["ts"][599],
        prefix_hash=price_hash(bars.head(600)),
        catalog_hash="new-engine",
        validation_sharpe=1.0,
        robustness=1.0,
    )
    monkeypatch.setattr(service.prices, "update", lambda *args, **kwargs: bars)
    monkeypatch.setattr(service, "_selection", lambda *args, **kwargs: (changed, None))
    cohort = {
        "candidate_hash": "c",
        "catalog_hash": "old-engine",
        "version_id": "frozen-version",
        "created_session": "2026-01-01",
        "members": [
            {
                "ticker": "AAPL",
                "sector": "Technology",
                "selection": {
                    "strategy": strategy.model_dump(mode="json"),
                    "prefix_hash": changed.prefix_hash,
                    "catalog_hash": "old-engine",
                },
            }
        ],
    }
    result = service._build_model(date(2026, 9, 24), 5, cohort)
    assert result["qualified"] is False
    assert result["reason"] == "cohort_requalification_failed"


def test_post_freeze_split_creates_audited_cohort_version(tmp_path, monkeypatch):
    service = ForecastService(tmp_path, NoNetwork())
    service.initialize()
    bars = _bars(date(2026, 9, 24)).with_columns(
        pl.when(pl.col("ts") == date(2026, 9, 24))
        .then(pl.lit(2.0))
        .otherwise(pl.col("stock_splits"))
        .alias("stock_splits")
    )
    strategy = generate_strategies(1, seed=7)[0]
    _, current_catalog_hash = catalog()
    changed = Selection(
        strategy=strategy,
        cutoff=bars["ts"][599],
        prefix_hash=price_hash(bars.head(600)),
        catalog_hash=current_catalog_hash,
        validation_sharpe=1.0,
        robustness=1.0,
    )
    frozen = {
        "candidate_hash": "c",
        "catalog_hash": "previous-engine",
        "created_session": "2026-01-01",
        "members": [
            {
                "ticker": "AAPL",
                "sector": "Technology",
                "selection": {
                    "strategy": strategy.model_dump(mode="json"),
                    "prefix_hash": "original-prefix",
                    "catalog_hash": "previous-engine",
                },
            }
        ],
    }
    service.repository.save_cohort("c", "previous-engine", frozen)
    original = service.repository.get_cohort()
    monkeypatch.setattr(service.prices, "update", lambda *args, **kwargs: bars)
    monkeypatch.setattr(service, "_selection", lambda *args, **kwargs: (changed, None))
    monkeypatch.setattr(
        "stocksweeper.forecast.service.strategy_states", lambda *args: ("long",) * 700
    )
    monkeypatch.setattr(
        "stocksweeper.forecast.service.fit_audit",
        lambda *args: CalibrationResult(
            fit_values=(-1.0, 0.0, 1.0),
            baseline_values=(-1.0, 0.0, 1.0),
            fit_peers=60,
            audit_peers=20,
            audit_blocks=40,
            crps_skill_lower_90=0.02,
            brier_delta=-0.01,
            support_low=-2.0,
            support_high=2.0,
            qualified=True,
            reason="passed",
        ),
    )
    result, reason = service._active_model(date(2026, 9, 24), 5, original)
    latest = service.repository.get_cohort()
    assert reason == "passed"
    assert result is not None
    assert result["qualified"] is True
    assert latest["version_id"] != original["version_id"]
    assert result["cohort_version_id"] == latest["version_id"]
    assert result["cohort_revisions"][0]["split_sessions_since_freeze"] == ["2026-09-24"]
    assert latest["members"][0]["selection"]["prefix_hash"] == changed.prefix_hash
    assert latest["catalog_hash"] == current_catalog_hash
    assert (
        service.repository.get_build("2026-09", 5, latest["version_id"], current_catalog_hash)[
            "model_id"
        ]
        == result["model_id"]
    )


def test_unrefreshed_model_retires_after_90_completed_sessions(tmp_path, monkeypatch):
    service = ForecastService(tmp_path, NoNetwork())
    service.initialize()
    _, current_catalog_hash = catalog()
    cohort = {
        "candidate_hash": "c",
        "catalog_hash": current_catalog_hash,
        "version_id": "frozen-version",
        "members": [],
    }
    service.repository.save_build(
        "2026-01",
        5,
        "frozen-version",
        current_catalog_hash,
        {
            "model_id": "old-model",
            "qualified": True,
            "activated_session": "2026-01-05",
            "data_hash": "old-data",
            "cohort_version_id": "frozen-version",
            "states": {},
        },
    )
    monkeypatch.setattr(
        service,
        "_build_model",
        lambda *args: service._failed_build(date(2026, 9, 24), 5, "validation_failed"),
    )
    active, reason = service._active_model(date(2026, 9, 24), 5, cohort)
    assert active is None
    assert reason == "stale_model"


def test_transient_monthly_build_retries_without_reusing_failed_audit(tmp_path, monkeypatch):
    service = ForecastService(tmp_path, NoNetwork())
    service.initialize()
    _, current_catalog_hash = catalog()
    cohort = {
        "candidate_hash": "original-listing-hash",
        "catalog_hash": current_catalog_hash,
        "version_id": "frozen-version",
        "members": [],
    }
    calls = []

    def build(*args):
        calls.append(len(calls))
        if len(calls) == 1:
            return service._failed_build(date(2026, 9, 24), 5, "peer_data_missing")
        return {
            "model_id": "recovered-model",
            "qualified": True,
            "reason": "passed",
            "activated_session": "2026-09-24",
            "data_hash": "recovered-data",
            "cohort_version_id": "frozen-version",
            "states": {},
        }

    monkeypatch.setattr(service, "_build_model", build)
    first, reason = service._active_model(date(2026, 9, 24), 5, cohort)
    assert first is None and reason == "peer_data_missing"
    assert service._active_model(date(2026, 9, 24), 5, cohort)[0] is None
    assert len(calls) == 1
    recovered, reason = service._active_model(date(2026, 9, 24), 5, cohort, force_rebuild=True)
    assert recovered is not None and recovered["model_id"] == "recovered-model"
    assert reason == "passed"
    assert len(calls) == 2
    assert (
        service.repository.get_build("2026-09", 5, "frozen-version", current_catalog_hash)[
            "model_id"
        ]
        == "recovered-model"
    )


def test_failed_audit_exposes_current_signal_gate_reason(tmp_path, monkeypatch):
    service = ForecastService(tmp_path, NoNetwork())
    service.initialize()
    completed = date(2026, 9, 24)
    expiry = date(2026, 10, 1)
    horizon = service.calendar.horizon(completed, expiry)
    bars = _bars(completed)
    strategy = generate_strategies(1, seed=7)[0]
    selection = Selection(
        strategy=strategy,
        cutoff=bars["ts"][599],
        prefix_hash=price_hash(bars.head(600)),
        catalog_hash="catalog",
        validation_sharpe=1.0,
        robustness=1.0,
    )
    _, current_catalog_hash = catalog()
    model = {
        "model_id": "failed-audit",
        "qualified": False,
        "reason": "validation_failed",
        "activated_session": completed.isoformat(),
        "data_hash": "peer-data",
        "cohort_version_id": "cohort-one",
        "cohort_size": 80,
        "states": {
            "long": {
                "qualified": False,
                "reason": "insufficient_audit_blocks",
                "fit_peers": 60,
                "audit_peers": 20,
                "fit_samples": 500,
                "audit_samples": 20,
                "audit_blocks": 12,
                "crps_skill_lower_90": None,
                "brier_delta": None,
            },
            "flat": {"qualified": False, "reason": "crps_audit_failed"},
        },
    }
    service.repository.save_build("2026-09", horizon, "cohort-one", current_catalog_hash, model)
    monkeypatch.setattr(service.prices, "update", lambda *args, **kwargs: bars)
    monkeypatch.setattr(service, "_selection", lambda *args, **kwargs: (selection, None))
    monkeypatch.setattr(
        "stocksweeper.forecast.service.strategy_states", lambda *args: ("long",) * 700
    )
    monkeypatch.setattr(
        "stocksweeper.forecast.service.current_state_and_volatility",
        lambda *args: ("long", 0.02, 100.0),
    )
    monkeypatch.setattr(
        service,
        "_cohort",
        lambda *args: {
            "version_id": "cohort-one",
            "catalog_hash": current_catalog_hash,
            "members": [{"ticker": "MSFT"}],
        },
    )
    snapshot = service.refresh_contract(
        "AAPL",
        "call",
        Decimal("100"),
        expiry,
        as_of=datetime(2026, 9, 24, 23, tzinfo=UTC),
        candidates=[],
    )
    assert snapshot.status == "unavailable"
    assert snapshot.itm_probability is None
    assert snapshot.reason == "insufficient_audit_blocks"
    assert snapshot.audit_blocks == 12


def test_audited_tails_are_strict_and_monotonic(tmp_path, monkeypatch):
    service = ForecastService(tmp_path, NoNetwork())
    service.initialize()
    completed = date(2026, 9, 24)
    bars = _bars(completed)
    strategy = generate_strategies(1, seed=7)[0]
    selection = Selection(
        strategy=strategy,
        cutoff=bars["ts"][599],
        prefix_hash=price_hash(bars.head(600)),
        catalog_hash="catalog",
        validation_sharpe=1.0,
        robustness=1.0,
    )
    evidence = {
        "qualified": True,
        "reason": "passed",
        "fit_values": [-2.0, -1.0, 0.0, 1.0, 2.0],
        "support_low": -10.0,
        "support_high": 10.0,
        "fit_peers": 60,
        "audit_peers": 20,
        "fit_samples": 500,
        "audit_samples": 200,
        "audit_blocks": 45,
        "crps_skill_lower_90": 0.03,
        "brier_delta": -0.01,
    }
    model = {
        "model_id": "audited-model",
        "data_hash": "peer-data",
        "states": {"long": evidence},
        "cohort_size": 80,
    }
    monkeypatch.setattr(service.prices, "update", lambda *args, **kwargs: bars)
    monkeypatch.setattr(service, "_selection", lambda *args, **kwargs: (selection, None))
    monkeypatch.setattr(
        "stocksweeper.forecast.service.strategy_states",
        lambda *args: ("long",) * 700,
    )
    monkeypatch.setattr(
        "stocksweeper.forecast.service.current_state_and_volatility",
        lambda *args: ("long", 0.02, 100.0),
    )
    monkeypatch.setattr(service, "_cohort", lambda *args: {"members": [{"ticker": "MSFT"}]})
    monkeypatch.setattr(service, "_active_model", lambda *args: (model, "passed"))
    expiry = date(2026, 10, 1)
    as_of = datetime(2026, 9, 24, 23, tzinfo=UTC)
    calls = [
        service.refresh_contract(
            "AAPL",
            "call",
            Decimal(str(strike)),
            expiry,
            as_of=as_of,
            candidates=[],
        ).itm_probability
        for strike in (99, 100, 101)
    ]
    puts = [
        service.refresh_contract(
            "AAPL",
            "put",
            Decimal(str(strike)),
            expiry,
            as_of=as_of,
            candidates=[],
        ).itm_probability
        for strike in (99, 100, 101)
    ]
    assert calls[0] >= calls[1] >= calls[2]
    assert puts[0] <= puts[1] <= puts[2]
    assert calls[1] == puts[1] == 0.4
