"""Adversarial cohort and cache boundaries for daily evidence reuse."""

from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import stocksweeper.forecast.evidence_reports as reports
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.forecast.physical_evaluation import evaluate_band
from stocksweeper.forecast.predictive import BASELINE_VERSION
from stocksweeper.storage.db import connect

from .test_forecast_ledger import EXPIRY, T0, issue, label


CANDIDATE = "student_t_ewma"
AS_OF = datetime(2026, 11, 2, 12, tzinfo=UTC)


def _record_attempt(ledger, digest, strike, method, **updates):
    expiry = SessionCalendar().offset(T0.date(), 1)
    key = f"w1:IREN:IREN:call:{expiry}:{strike}.000"
    version = BASELINE_VERSION if method == "lognormal_ewma" else reports.CANDIDATE_VERSIONS[method]
    ledger.record(issue(
        digest, contract_key=key, strike_exact=f"{strike}.000", expiration=expiry,
        expiry_session=expiry, method=method, model_version=version, **updates,
    ))
    ledger.record_label(replace(
        label(checked_at=AS_OF), contract_key=key, expiry_session=expiry,
        classification="itm" if strike < 42 else "atm" if strike == 42 else "otm",
    ))


def test_uncovered_earlier_baseline_does_not_select_candidate_input_vintage(tmp_path):
    ledger = ForecastLedger(tmp_path)
    digest = ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    _record_attempt(ledger, digest, 43, "lognormal_ewma", data_hash="a" * 64)
    _record_attempt(
        ledger, digest, 41, "lognormal_ewma", data_hash="b" * 64,
        issued_at=T0 + timedelta(minutes=2), itm_probability=0.4, otm_probability=0.6,
    )
    _record_attempt(
        ledger, digest, 41, CANDIDATE, data_hash="b" * 64,
        issued_at=T0 + timedelta(minutes=3), itm_probability=0.7, otm_probability=0.3,
    )
    calendar = SessionCalendar()
    common = {"as_of": AS_OF, "current_version_only": True}
    baseline = reports.ledger_band_rows(
        ledger, calendar, "as_issued", "lognormal_ewma", None, "all", "1", **common,
    )
    paired = reports.ledger_band_rows(
        ledger, calendar, "as_issued", CANDIDATE, None, "all", "1", **common,
    )
    reused = reports.ledger_band_rows(
        ledger, calendar, "as_issued", CANDIDATE, None, "all", "1",
        baseline_rows=tuple(baseline), **common,
    )
    assert reused == paired
    assert {row.strike for row in reused} == {"41.000"}
    report = evaluate_band(reused, CANDIDATE, "1", bootstrap_samples=100)
    assert report["ticker_origin_horizon_units"] == 1
    assert report["later_vintage_attempts_excluded"] == 0
    assert report["brier"]["baseline"] == pytest.approx(0.36)
    assert report["brier"]["candidate"] == pytest.approx(0.09)


def test_empty_baseline_snapshot_is_reused_instead_of_rescanning(tmp_path, monkeypatch):
    ledger = ForecastLedger(tmp_path)
    digest = ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    _record_attempt(ledger, digest, 41, "lognormal_ewma")
    _record_attempt(ledger, digest, 41, CANDIDATE)
    scans = []
    original = ledger.iter_evaluation_rows

    def counted(**kwargs):
        scans.append(kwargs["methods"])
        return original(**kwargs)

    monkeypatch.setattr(ledger, "iter_evaluation_rows", counted)
    calendar = SessionCalendar()
    reused = reports.ledger_band_rows(
        ledger, calendar, "as_issued", CANDIDATE, None, "all", "1",
        as_of=AS_OF, current_version_only=True, baseline_rows=[],
    )
    fallback = reports.ledger_band_rows(
        ledger, calendar, "as_issued", CANDIDATE, None, "all", "1",
        as_of=AS_OF, current_version_only=True, baseline_rows=None,
    )
    assert scans == [(CANDIDATE,), ("lognormal_ewma", CANDIDATE)]
    assert [row.method for row in reused] == [CANDIDATE]
    assert {row.method for row in fallback} == {"lognormal_ewma", CANDIDATE}
    assert evaluate_band(reused, CANDIDATE, "1")["rejection_reasons"] == {
        "baseline_not_issued": 1,
    }


@pytest.mark.parametrize("baseline_count", [0, 2, 3])
def test_build_reuses_through_cap_and_falls_back_only_above_it(
    tmp_path, monkeypatch, baseline_count,
):
    assert reports._BASELINE_REUSE_LIMIT == 10_000
    monkeypatch.setattr(reports, "_BASELINE_REUSE_LIMIT", 2)
    monkeypatch.setattr(reports, "CANDIDATE_VERSIONS", {
        CANDIDATE: reports.CANDIDATE_VERSIONS[CANDIDATE],
    })
    ledger = ForecastLedger(tmp_path)
    digest = ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    for strike in range(41, 41 + baseline_count):
        _record_attempt(ledger, digest, strike, "lognormal_ewma")
    _record_attempt(ledger, digest, 41, CANDIDATE)
    oracle = reports.ledger_contest(
        ledger, SessionCalendar(), "as_issued", CANDIDATE, None, "all",
        as_of=AS_OF, since=AS_OF.date() - timedelta(days=1096), current_version_only=True,
    )
    scans = []
    original = ledger.iter_evaluation_rows

    def counted(**kwargs):
        if kwargs.get("with_crps") and CANDIDATE in kwargs["methods"]:
            scans.append((kwargs["horizon_range"], kwargs["methods"]))
        return original(**kwargs)

    monkeypatch.setattr(ledger, "iter_evaluation_rows", counted)
    evidence = reports.build_model_evidence(tmp_path, ledger, AS_OF)
    first_methods = ("lognormal_ewma", CANDIDATE) if baseline_count > 2 else (CANDIDATE,)
    assert scans == [((1, 1), first_methods), ((2, 5), (CANDIDATE,)), ((6, 25), (CANDIDATE,))]
    for band in reports.BANDS:
        prospective = evidence[(CANDIDATE, band)]["prospective"]
        assert prospective["report_hash"] == reports._digest(oracle)
        for metric in ("baseline", "candidate", "paired_delta"):
            assert prospective["brier"][metric] == oracle["bands"][band]["brier"][metric]
        assert prospective["rejection_reasons"] == oracle["bands"][band]["rejection_reasons"]


def test_more_than_cache_capacity_of_real_scores_survive_eviction_across_pages(tmp_path):
    ledger = ForecastLedger(tmp_path)
    digest = ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    terms = issue(digest).terms_note
    with connect(ledger.path) as db:
        db.execute(
            """INSERT INTO forecast_issuances (
               idempotency_key, contract_key, ticker, root, side, expiration,
               expiry_session, strike_exact, terms_note, input_session, issued_at,
               method, distribution_hash, status, provenance)
               SELECT lpad(CAST(i AS VARCHAR), 5, '0'),
                      'w1:IREN:IREN:call:2026-10-30:' || CAST(i + 1 AS VARCHAR),
                      'IREN', 'IREN', 'call', ?, ?, CAST(i + 1 AS VARCHAR),
                      ?, ?, ?, 'lognormal_ewma', ?, 'available', 'as_issued'
               FROM range(4097) AS contracts(i)""",
            [EXPIRY, EXPIRY, terms, T0.date(), T0, digest],
        )
        db.execute(
            """INSERT INTO forecast_labels (
               idempotency_key, contract_key, terms_note, expiry_session,
               checked_at, status, selected_close_exact, classification)
               SELECT 'label-' || i,
                      'w1:IREN:IREN:call:2026-10-30:' || CAST(i + 1 AS VARCHAR),
                      ?, ?, ?, 'valid', CAST(2098 + i AS VARCHAR), 'itm'
               FROM range(4097) AS contracts(i)""",
            [terms, EXPIRY, AS_OF],
        )
    # Every close is above both terminal prices: E|X-y| - E|X-X'|/2 = y - 43.36.
    cache = OrderedDict(((digest, str(close)), close - 43.36) for close in range(50, 4146))
    scored = list(ledger.iter_evaluation_rows(with_crps=True, page_size=4096, crps_cache=cache))
    assert len(scored) == 4097
    assert all(
        row["crps"] == pytest.approx(float(row["selected_close_exact"]) - 43.36)
        for row in scored
    )
    assert len(cache) == 4096
    assert (digest, "2098") not in cache
    assert next(reversed(cache)) == (digest, "6194")


def test_shared_lru_keeps_successes_and_retries_missing_scores_in_next_iterator(
    tmp_path, monkeypatch,
):
    ledger = ForecastLedger(tmp_path)
    good = ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    absent = ledger.record_distribution((41.0, 45.0), (0.4, 0.6), T0)
    for strike, digest in ((40, good), (41, absent), (42, absent), (43, good)):
        key = f"w1:IREN:IREN:call:{EXPIRY}:{strike}.000"
        ledger.record(issue(digest, contract_key=key, strike_exact=f"{strike}.000"))
        ledger.record_label(replace(
            label(checked_at=AS_OF), contract_key=key,
            classification="itm" if strike < 42 else "atm" if strike == 42 else "otm",
        ))
    with connect(ledger.path) as db:
        db.execute("DELETE FROM forecast_distributions WHERE distribution_hash = ?", [absent])
    requested = []
    original = ledger.crps_scores

    def counted(pairs):
        pairs = set(pairs)
        requested.extend(pairs)
        return original(pairs)

    monkeypatch.setattr(ledger, "crps_scores", counted)
    cache = OrderedDict()
    first = list(ledger.iter_evaluation_rows(with_crps=True, page_size=1, crps_cache=cache))
    assert all(
        row["crps"] == pytest.approx(1.04) if row["distribution_hash"] == good
        else row["crps"] is None for row in first
    )
    assert set(cache) == {(good, "42")}
    assert requested.count((good, "42")) == requested.count((absent, "42")) == 1
    ledger.record_distribution((41.0, 45.0), (0.4, 0.6), T0)
    requested.clear()
    second = list(ledger.iter_evaluation_rows(with_crps=True, page_size=1, crps_cache=cache))
    assert all(row["crps"] is not None for row in second)
    assert all(
        row["crps"] == pytest.approx(1.24) for row in second
        if row["distribution_hash"] == absent
    )
    assert requested == [(absent, "42")]
    assert set(cache) == {(good, "42"), (absent, "42")}


def test_shared_lru_hit_is_promoted_before_new_success_evicts_oldest(tmp_path):
    ledger = ForecastLedger(tmp_path)
    digest = ledger.record_distribution((40.0, 44.0), (0.4, 0.6), T0)
    ledger.record(issue(digest))
    ledger.record_label(label(checked_at=AS_OF))
    second_key = f"w1:IREN:IREN:call:{EXPIRY}:42.000"
    ledger.record(issue(digest, contract_key=second_key, strike_exact="42.000"))
    ledger.record_label(replace(label(checked_at=AS_OF, close="44"), contract_key=second_key))
    cache = OrderedDict([((digest, "42"), 1.04)])
    cache.update(((digest, str(close)), close - 43.36) for close in range(50, 4145))
    scored = list(ledger.iter_evaluation_rows(with_crps=True, crps_cache=cache))
    assert {row["selected_close_exact"]: row["crps"] for row in scored} == pytest.approx({
        "42": 1.04, "44": 0.64,
    })
    assert len(cache) == 4096
    assert (digest, "42") in cache
    assert (digest, "50") not in cache
    assert next(reversed(cache)) == (digest, "44")
