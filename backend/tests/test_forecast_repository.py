from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from stocksweeper.forecast.models import ForecastSnapshot
from stocksweeper.forecast.repository import ForecastRepository


def test_forecast_evidence_and_versions_initialize_without_manual_run_tables(tmp_path) -> None:
    repository = ForecastRepository(tmp_path)
    repository.initialize()
    repository.save_selection("AAPL", "prefix-v1", "catalog-v1", {"strategy_id": "rule-1"})
    repository.save_cohort("candidates-v1", "catalog-v1", {"tickers": ["AAPL", "MSFT"]})
    repository.save_cohort("candidates-v2", "catalog-v2", {"tickers": ["OTHER"]})
    repository.save_cohort_attempt(
        "2026-09",
        "candidates-v1",
        "catalog-v1",
        {"qualified_count": 24, "reason": "cohort_insufficient"},
    )
    assert repository.get_selection("AAPL", "prefix-v1", "catalog-v1") == {"strategy_id": "rule-1"}
    assert repository.get_selection("AAPL", "prefix-v2", "catalog-v1") is None
    assert repository.get_cohort()["tickers"] == ["AAPL", "MSFT"]
    assert len(repository.get_cohort()["version_id"]) == 64
    assert repository.get_cohort_attempt("2026-09", "candidates-v1", "catalog-v1") == {
        "qualified_count": 24,
        "reason": "cohort_insufficient",
    }
    assert repository.get_cohort_attempt("2026-10", "candidates-v1", "catalog-v1") is None
    repository.save_cohort_attempt(
        "2026-09",
        "candidates-v1",
        "catalog-v1",
        {"qualified_count": 31, "reason": "provider_recovered"},
    )
    restarted = ForecastRepository(tmp_path)
    restarted.initialize()
    assert restarted.get_cohort_attempt("2026-09", "candidates-v1", "catalog-v1") == {
        "qualified_count": 31,
        "reason": "provider_recovered",
    }

    failed = {"model_id": "failed-v1", "qualified": False, "reason": "sparse_state"}
    qualified = {
        "model_id": "passed-v1",
        "qualified": True,
        "data_hash": "bars-v1",
        "fit_peers": 62,
        "audit_peers": 18,
    }
    repository.save_build("2026-09", 5, "candidates-v1", "catalog-v1", failed)
    repository.save_build("2026-09", 10, "candidates-v1", "catalog-v1", qualified)
    assert repository.get_build("2026-09", 5, "candidates-v1", "catalog-v1") == failed
    assert repository.latest_qualified(5) is None
    assert repository.latest_qualified(10) == qualified
    assert repository.get_build("2026-09", 10, "candidates-v1", "catalog-v1") == qualified

    with duckdb.connect(str(tmp_path / "results.duckdb"), read_only=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'runs'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM forecast_model_versions").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM forecast_evidence").fetchone()[0] == 2


def test_existing_manual_run_table_is_preserved_for_existing_local_data(tmp_path) -> None:
    path = tmp_path / "results.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE runs (id VARCHAR PRIMARY KEY)")
        connection.execute("INSERT INTO runs VALUES ('old-run')")

    repository = ForecastRepository(tmp_path)
    repository.initialize()
    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT id FROM runs").fetchall() == [("old-run",)]


def test_builds_append_revisions_and_reject_model_identity_collision(tmp_path) -> None:
    repository = ForecastRepository(tmp_path)
    repository.initialize()
    original = {"model_id": "model-v1", "qualified": True, "data_hash": "bars-v1"}
    revised = {"model_id": "model-v2", "qualified": True, "data_hash": "bars-v2"}
    assert repository.save_build("2026-09", 1, "candidates", "catalog", original) == "model-v1"
    assert repository.save_build("2026-09", 1, "candidates", "catalog", original) == "model-v1"
    assert repository.save_build("2026-09", 1, "candidates", "catalog", revised) == "model-v2"
    assert repository.get_build("2026-09", 1, "candidates", "catalog") == revised
    with pytest.raises(ValueError, match="different evidence"):
        repository.save_build(
            "2026-09",
            1,
            "candidates",
            "catalog",
            {"model_id": "model-v1", "qualified": True, "data_hash": "different"},
        )


def test_cohort_versions_keep_members_frozen_but_allow_audited_reselection(tmp_path) -> None:
    repository = ForecastRepository(tmp_path)
    repository.initialize()
    original = {
        "candidate_hash": "candidate-list",
        "catalog_hash": "engine-v1",
        "members": [
            {"ticker": "AAPL", "sector": "Technology", "selection": {"strategy_id": "rule-1"}},
            {"ticker": "EXPE", "sector": "Consumer", "selection": {"strategy_id": "rule-2"}},
        ],
    }
    repository.save_cohort("candidate-list", "engine-v1", original)
    first = repository.get_cohort()
    assert first is not None
    revised = {
        **original,
        "members": [
            {**original["members"][0], "selection": {"strategy_id": "rule-3"}},
            original["members"][1],
        ],
    }
    second_id = repository.save_cohort_version(revised)
    assert second_id != first["version_id"]
    assert repository.save_cohort_version(revised) == second_id
    reopened = ForecastRepository(tmp_path)
    reopened.initialize()
    assert reopened.get_cohort() == {**revised, "version_id": second_id}
    with pytest.raises(ValueError, match="members and sectors"):
        repository.save_cohort_version({**revised, "members": revised["members"][:1]})
    with pytest.raises(ValueError, match="candidate identity"):
        repository.save_cohort_version({**revised, "candidate_hash": "new-candidates"})


def test_daily_snapshots_deduplicate_retries_but_preserve_corrections(tmp_path) -> None:
    repository = ForecastRepository(tmp_path)
    repository.initialize()
    observed = datetime(2026, 9, 24, 20, tzinfo=UTC)
    base = ForecastSnapshot(
        ticker="AAPL",
        side="call",
        strike=Decimal("200.000"),
        expiry=date(2026, 10, 16),
        as_of=date(2026, 9, 24),
        status="available",
        itm_probability=0.6,
        reason=None,
        model_id="model-v1",
        data_hash="bars-v1",
        created_at=observed,
    )
    repository.save_snapshot(base)
    retry = base.model_copy(update={"created_at": observed + timedelta(minutes=1)})
    repository.save_snapshot(retry)
    revised = base.model_copy(
        update={
            "itm_probability": 0.58,
            "data_hash": "bars-v2",
            "created_at": observed + timedelta(minutes=2),
        }
    )
    repository.save_snapshot(revised)
    later = base.model_copy(
        update={
            "status": "unavailable",
            "itm_probability": None,
            "reason": "model_retired",
            "as_of": date(2026, 9, 25),
            "created_at": observed + timedelta(days=1),
        }
    )
    repository.save_snapshot(later)
    reopened = ForecastRepository(tmp_path)
    reopened.initialize()
    assert reopened.get_cohort_attempt("2026-09", "candidates", "catalog") is None
    assert reopened.latest_snapshot("AAPL", "call", Decimal("200"), date(2026, 10, 16)) == later
    assert (
        reopened.last_available_snapshot("AAPL", "call", Decimal("200"), date(2026, 10, 16))
        == revised
    )
    assert reopened.latest_snapshot("AAPL", "put", Decimal("200"), date(2026, 10, 16)) is None
    with duckdb.connect(str(tmp_path / "results.duckdb"), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM forecast_snapshots").fetchone()[0] == 3
