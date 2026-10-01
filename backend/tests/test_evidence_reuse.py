"""Reuse changes execution cost, never the evidence cohort or scores."""

from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

import stocksweeper.forecast.evidence_reports as reports
import stocksweeper.forecast.physical_evaluation as evaluation
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.forecast.predictive import BASELINE_VERSION
from stocksweeper.storage.db import connect

from .test_forecast_ledger import EXPIRY, T0, issue, label


def test_build_score_cache_is_bounded_and_eviction_keeps_page_scores(tmp_path):
    ledger = ForecastLedger(tmp_path)
    digest = ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    ledger.record(issue(digest))
    ledger.record_label(label(checked_at=datetime(2026, 10, 31, 12, tzinfo=UTC)))
    cache = OrderedDict(((f"{i:064x}", "42"), 99.0) for i in range(4096))
    page = list(ledger.iter_evaluation_rows(with_crps=True, crps_cache=cache))
    assert page[0]["crps"] == pytest.approx(1.04)
    assert len(cache) == 4096
    assert next(iter(cache)) == (f"{1:064x}", "42")
    assert cache[(digest, "42")] == pytest.approx(1.04)


def test_missing_distribution_is_retried_in_next_iterator(tmp_path):
    ledger = ForecastLedger(tmp_path)
    digest = ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    ledger.record(issue(digest))
    ledger.record_label(label(checked_at=datetime(2026, 10, 31, 12, tzinfo=UTC)))
    with connect(ledger.path) as db:
        db.execute("DELETE FROM forecast_distributions")
    cache = OrderedDict()
    assert next(ledger.iter_evaluation_rows(with_crps=True, crps_cache=cache))["crps"] is None
    assert not cache
    ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    assert next(ledger.iter_evaluation_rows(with_crps=True, crps_cache=cache))[
        "crps"
    ] == pytest.approx(1.04)


def _populate(ledger):
    calendar = SessionCalendar()
    origin = date(2026, 9, 25)
    issued = datetime(2026, 9, 25, 22, tzinfo=UTC)
    digest = ledger.record_distribution((80.0, 120.0), (0.4, 0.6), issued)
    versions = {"lognormal_ewma": BASELINE_VERSION, **reports.CANDIDATE_VERSIONS}
    for horizon in (1, 3, 8):
        expiry = calendar.offset(origin, horizon)
        for side in ("call", "put"):
            key = f"w1:IREN:IREN:{side}:{expiry}:41.000"
            for method, version in versions.items():
                # Same timestamp exercises issuance-key tie order, unavailable
                # versions and later vintages must survive adaptation unchanged.
                for data_hash, offset in (("a" * 64, 0), ("b" * 64, 1)):
                    ledger.record(
                        issue(
                            digest,
                            contract_key=key,
                            side=side,
                            expiration=expiry,
                            expiry_session=expiry,
                            method=method,
                            model_version=version,
                            data_hash=data_hash,
                            issued_at=issued + timedelta(minutes=offset),
                        )
                    )
                ledger.record(
                    issue(
                        digest,
                        contract_key=key,
                        side=side,
                        expiration=expiry,
                        expiry_session=expiry,
                        method=method,
                        model_version="obsolete-v0",
                    )
                )
            ledger.record(
                issue(
                    None,
                    contract_key=key,
                    side=side,
                    expiration=expiry,
                    expiry_session=expiry,
                    method="student_t_ewma",
                    model_version=reports.CANDIDATE_VERSIONS["student_t_ewma"],
                    status="unavailable",
                    itm_probability=None,
                    otm_probability=None,
                    atm_probability=None,
                    unavailable_reason="fit_failed",
                    data_hash="c" * 64,
                )
            )
            ledger.record_label(
                replace(
                    label(checked_at=datetime(2026, 11, 1, 12, tzinfo=UTC)),
                    contract_key=key,
                    expiry_session=expiry,
                    classification="itm" if side == "call" else "otm",
                )
            )
            ledger.record_label(
                replace(
                    label(checked_at=datetime(2026, 11, 3, 12, tzinfo=UTC), status="excluded"),
                    contract_key=key,
                    expiry_session=expiry,
                )
            )
    # Missing scalar inputs and orphan candidate are kept by the original oracle.
    ledger.record(
        issue(
            None,
            method=None,
            model_version=None,
            status="unavailable",
            itm_probability=None,
            otm_probability=None,
            atm_probability=None,
            unavailable_reason="missing_history",
        )
    )
    ledger.record(
        issue(
            digest,
            ticker="OTHER",
            root="OTHER",
            contract_key=f"w1:OTHER:OTHER:call:{EXPIRY}:41.000",
            method="student_t_ewma",
            model_version=reports.CANDIDATE_VERSIONS["student_t_ewma"],
        )
    )


@pytest.mark.parametrize(
    "as_of", [datetime(2026, 11, 2, 12, tzinfo=UTC), datetime(2026, 11, 4, 12, tzinfo=UTC)]
)
@pytest.mark.parametrize("cap", [10_000, 0])
def test_daily_build_matches_standalone_oracle_and_preserves_return_order(
    tmp_path, monkeypatch, as_of, cap
):
    ledger = ForecastLedger(tmp_path)
    _populate(ledger)
    calendar = SessionCalendar()
    since = as_of.date() - timedelta(days=1096)
    expected = {}
    for candidate in reports.CANDIDATE_VERSIONS:
        report = reports.ledger_contest(
            ledger,
            calendar,
            "as_issued",
            candidate,
            None,
            "all",
            as_of=as_of,
            since=since,
            current_version_only=True,
        )
        expected[candidate] = report
    monkeypatch.setattr(reports, "_BASELINE_REUSE_LIMIT", cap)
    result = reports.build_model_evidence(tmp_path, ledger, as_of)
    assert list(result) == [
        (method, band)
        for method in ("lognormal_ewma", *reports.CANDIDATE_VERSIONS, "intraday_shadow")
        for band in reports.BANDS
    ]
    for candidate, report in expected.items():
        for band in reports.BANDS:
            assert result[(candidate, band)]["prospective"] == reports._summary(
                report["bands"][band],
                provenance="as_issued",
                generated_at=as_of.isoformat(),
                report_hash=reports._digest(report),
                model_version=reports.CANDIDATE_VERSIONS[candidate],
                evidence_window_start=since,
                coverage_basis="recorded_current_version_candidate_cells",
            )


def test_daily_reuse_scores_shared_pair_once_and_reads_metadata_once(tmp_path, monkeypatch):
    ledger = ForecastLedger(tmp_path)
    _populate(ledger)
    counts = {"crps": 0, "metadata": 0, "panel": 0}
    original = evaluation.crps

    def score(*args):
        counts["crps"] += 1
        return original(*args)

    def count(name, method):
        def wrapped(*args, **kwargs):
            counts[name] += 1
            return method(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(evaluation, "crps", score)
    monkeypatch.setattr(
        ledger, "evaluation_skipped_attempts", count("metadata", ledger.evaluation_skipped_attempts)
    )
    monkeypatch.setattr(ledger, "panel_coverage", count("panel", ledger.panel_coverage))
    reports.build_model_evidence(tmp_path, ledger, datetime(2026, 11, 2, 12, tzinfo=UTC))
    assert counts == {"crps": 1, "metadata": 1, "panel": 1}
