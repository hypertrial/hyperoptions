"""Opt-in offline audit measurements; run with Python 3.12 and PYTHONPATH=backend/src.

All price inputs and ledgers are synthetic and temporary. No vendor is contacted.
Timing is diagnostic, never a CI assertion. Each phase has a 60-second wall-clock cap.
"""

from __future__ import annotations

import asyncio
import cProfile
import io
import json
import platform
import pstats
import signal
import statistics
import sys
import tracemalloc
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import polars as pl

from options_api import chain, market_sources
from options_api.contract_identity import make_watch_key
from options_api.greeks import empty_greeks
from options_api.memo import ContractMemo
from options_api.market_watch import PAGE_INPUT_TIMEOUT
from options_api.models import OptionChainResponse, OptionQuote
from options_api.outcomes import TERMS_NOTE
from options_api.predictive_watch import PredictiveWatchOdds
from stocksweeper.forecast import ledger as ledger_module
from stocksweeper.forecast import physical_evaluation
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.evidence_reports import CANDIDATE_VERSIONS, build_model_evidence
from stocksweeper.forecast.ledger import ForecastIssuance, ForecastLabel, ForecastLedger
from stocksweeper.forecast.market import PRICE_COLUMNS, ForecastPriceStore
from stocksweeper.forecast.predictive import BASELINE_VERSION, clean_completed, price_hash
from stocksweeper.storage.db import DB_LOCK

NOW = datetime(2026, 10, 1, 14, tzinfo=UTC)
SESSION = date(2026, 9, 30)
WARMUPS = 2
SAMPLES = 7


def measure(name, work):
    def expired(_signum, _frame):
        raise TimeoutError(f"{name} exceeded 60 seconds")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 60)
    elapsed = []
    try:
        for _ in range(WARMUPS):
            work()
        for _ in range(SAMPLES):
            start = perf_counter()
            counts = work()
            elapsed.append(1000 * (perf_counter() - start))
        tracemalloc.start()
        work()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        profile = cProfile.Profile()
        profile.runcall(work)
        output = io.StringIO()
        pstats.Stats(profile, stream=output).strip_dirs().sort_stats("cumulative").print_stats(8)
        return {
            "phase": name,
            "samples_ms": elapsed,
            "median_ms": statistics.median(elapsed),
            "min_ms": min(elapsed),
            "max_ms": max(elapsed),
            "operations_per_sample": counts,
            "python_peak_bytes_separate_sample": peak,
            "profile_separate_sample": output.getvalue(),
        }
    except TimeoutError as exc:
        return {"phase": name, "capped": str(exc), "completed_samples_ms": elapsed}
    finally:
        tracemalloc.stop()
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def synthetic_chain(size):
    rows = []
    for index in range(size):
        strike = Decimal("45") + Decimal(index) / 1000
        bid = max(Decimal("0.5"), Decimal("50.5") - strike)
        rows.append(OptionQuote(
            ticker="IREN", root="IREN", expiration="2026-10-16", strike=strike,
            call_bid=bid, call_ask=bid + Decimal("0.1"), call_open_interest=55,
        ))
    return OptionChainResponse(
        ticker="IREN", fetched_at=NOW, from_cache=False, last_trade="$50", spot=Decimal(50),
        rows=rows,
    )


def assembly_work(snapshot, memo, omit_greeks=False):
    calls = 0
    original = chain.compute_greeks

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return empty_greeks() if omit_greeks else original(*args, **kwargs)

    with patch.object(chain, "compute_greeks", counted):
        page = chain.assemble_covered_calls(
            snapshot, None, None, NOW.date(), NOW, "all", rate=Decimal("0.04"), memo=memo,
        )
    # main._load_page replaces these seven offline fields for every returned contract.
    fields = ("iv_pct_tenths", "delta_e4", "gamma_e4", "theta_e4", "vega_e4", "rho_e4")
    contracts = [contract for group in page.expirations for contract in group.contracts]
    for contract in contracts:
        for field in fields:
            setattr(contract, field, None)
        contract.greeks_source = None
    return {"rows": len(contracts), "greek_entry_calls": calls,
            "offline_greek_calculations": 0 if omit_greeks else calls,
            "replaced_greek_fields": 7}


def provenance_work(watch, digest, size):
    stats = 0
    original = Path.stat

    def counted(path, *args, **kwargs):
        nonlocal stats
        stats += 1
        return original(path, *args, **kwargs)

    with patch.object(Path, "stat", counted):
        for _ in range(size):
            assert watch.cache_retrieved_at("IREN", digest, SESSION) is not None
    return {"contracts": size, "filesystem_stat_calls": stats, "verified_cache_hits": size}


async def failing_treasury():
    calls = active = maximum = 0
    timeouts = set()

    async def fail(request):
        nonlocal calls, active, maximum
        calls += 1
        timeouts.add(request.extensions["timeout"]["read"])
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.005)
            raise httpx.ReadTimeout("synthetic outage", request=request)
        finally:
            active -= 1

    # Reset only this process's cache and lock, never the application or persisted data.
    with patch.object(market_sources, "_treasury_cache", None), patch.object(
        market_sources, "_treasury_lock", asyncio.Lock(),
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            results = await asyncio.gather(*[
                market_sources.fetch_treasury_curve(client, NOW) for _ in range(3)
            ])
    assert results == [None, None, None]
    return {
        "concurrent_callers": 3, "provider_attempts": calls, "maximum_active_attempts": maximum,
        "injected_attempt_delay_ms": 5, "configured_attempt_timeout_seconds": sorted(timeouts),
        "page_pricing_wait_seconds": PAGE_INPUT_TIMEOUT,
    }


def evidence_fixture(root):
    origin, expiry = date(2026, 9, 25), date(2026, 9, 28)
    issued = datetime(2026, 9, 25, 22, tzinfo=UTC)
    checked = datetime(2026, 9, 29, 22, tzinfo=UTC)
    key = make_watch_key("TEST", "TEST", "call", expiry.isoformat(), Decimal("100"))
    ledger = ForecastLedger(root)
    digest = ledger.record_distribution(
        tuple(80 + index / 100 for index in range(4096)), (1 / 4096,) * 4096, issued,
    )
    versions = {"lognormal_ewma": BASELINE_VERSION, **CANDIDATE_VERSIONS}
    ledger.record_batch((ForecastIssuance(
        key, "TEST", "TEST", "call", expiry, expiry, "100.000", TERMS_NOTE,
        origin, origin, issued, issued, version, method, "a" * 64, digest,
        "completed_close", "100", "available", 0.6, 0.4, 0, None,
    ), None) for method, version in versions.items())
    ledger.record_label(ForecastLabel(
        key, TERMS_NOTE, expiry, checked, "valid", None,
        "Nasdaq historical Close (Yahoo cross-check)", "101", "101", "101", "itm",
    ))
    return ledger


def evidence_work(ledger):
    counts = {"scalar_pages": 0, "scalar_rows_read": 0, "distribution_reads": 0,
              "crps_calls": 0, "crps_under_db_lock": 0}
    original_rows, original_crps = ledger_module.rows, physical_evaluation.crps

    def counted_rows(connection, sql, params=None):
        result = original_rows(connection, sql, params)
        if "WITH issues AS MATERIALIZED" in sql:
            counts["scalar_pages"] += 1
            counts["scalar_rows_read"] += len(result)
        if "FROM forecast_distributions" in sql:
            counts["distribution_reads"] += 1
        return result

    def counted_crps(*args):
        counts["crps_calls"] += 1
        counts["crps_under_db_lock"] += int(DB_LOCK.locked())
        return original_crps(*args)

    with patch.object(ledger_module, "rows", counted_rows), patch.object(
        physical_evaluation, "crps", counted_crps,
    ):
        reports = build_model_evidence(ledger.data_dir, ledger, NOW)
    return {"issuances": 1 + len(CANDIDATE_VERSIONS), "unique_distribution_close_pairs": 1,
            "scenarios": 4096, "reports": len(reports), **counts}


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Use the project's Python 3.12 runtime")
    reports = []
    with TemporaryDirectory(prefix="hyperoptions-audit-benchmark-") as directory:
        root = Path(directory)
        frame = pl.DataFrame({
            name: [SESSION] if name == "ts" else [0.0] if name in ("dividends", "stock_splits")
            else [1000.0] if name == "volume" else [50.0]
            for name in PRICE_COLUMNS
        }).with_columns(pl.col("ts").cast(pl.Date))
        provider = SimpleNamespace(fetch=lambda *_args: frame)
        saved = ForecastPriceStore(root, provider).update("IREN", SESSION)
        digest = price_hash(clean_completed(saved, SESSION, SessionCalendar()))
        watch = PredictiveWatchOdds(root, lambda: NOW, forecaster=SimpleNamespace(),
                                    refresh_enabled=False)
        assert watch.cache_retrieved_at("IREN", digest, SESSION) is not None
        for size in (250, 1000, 5000):
            reports.append(measure(f"provenance_hot_{size}",
                                   lambda size=size: provenance_work(watch, digest, size)))
        snapshot = synthetic_chain(1000)
        memo = ContractMemo()
        assembly_work(snapshot, memo)
        reports.append(measure("assembly_cold_1000", lambda: assembly_work(snapshot, None)))
        reports.append(measure("assembly_warm_1000", lambda: assembly_work(snapshot, memo)))
        reports.append(measure("assembly_cold_mocked_greeks_1000",
                               lambda: assembly_work(snapshot, None, omit_greeks=True)))
        reports.append(measure("treasury_three_failed_callers",
                               lambda: asyncio.run(failing_treasury())))
        ledger = ForecastLedger(root)
        prices = tuple(40 + index / 200 for index in range(4096))
        weights = (1 / 4096,) * 4096
        distribution = ledger.record_distribution(prices, weights, NOW)

        def score():
            result = ledger.crps_scores([(distribution, "50")])
            assert len(result) == 1
            return {"temporary_ledger_distributions": 1, "scenarios": 4096, "score_pairs": 1}

        reports.append(measure("crps_temporary_ledger_4096", score))
        evidence = evidence_fixture(root / "evidence")
        reports.append(measure("daily_evidence_shared_pair", lambda: evidence_work(evidence)))
    print(json.dumps({
        "runtime": platform.python_version(), "platform": platform.platform(),
        "warmups": WARMUPS, "samples": SAMPLES, "phase_cap_seconds": 60,
        "mode": "offline synthetic; cProfile and tracemalloc use separate samples",
        "measurements": reports,
    }, indent=2))


if __name__ == "__main__":
    main()
