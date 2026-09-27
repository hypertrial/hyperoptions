from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from options_api.contract_identity import make_watch_key
from options_api.models import OptionChainResponse, OptionQuote
from options_api.outcomes import TERMS_NOTE
from options_api import prospective_panel as panel_module
from options_api.prospective_panel import ProspectivePanel, select_contracts
from stocksweeper.forecast.audit import AUDIT_SIZE, AuditCohort, AuditMember
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.ledger import ForecastIssuance, ForecastLedger
from stocksweeper.forecast.predictive import PredictiveDistribution
from stocksweeper.storage.db import connect, rows


INPUT = date(2026, 9, 25)
SAMPLE_TIME = datetime(2026, 9, 28, 15, tzinfo=UTC)  # Monday, 11:00 ET.


def _row(ticker: str, expiry: date, strike: int, **changes: object) -> OptionQuote:
    fields = {
        "ticker": ticker,
        "expiration": expiry.isoformat(),
        "strike": Decimal(strike),
        "root": ticker,
        "identity_reason": None,
        "call_bid": None,
        "call_ask": None,
        "call_volume": 0,
        "call_open_interest": 0,
        "put_bid": None,
        "put_ask": None,
        "put_volume": 0,
        "put_open_interest": 0,
    }
    fields.update(changes)
    return OptionQuote.model_validate(fields)


def _chain(ticker: str, rows_: list[OptionQuote], **changes: object) -> OptionChainResponse:
    fields = {
        "ticker": ticker,
        "fetched_at": SAMPLE_TIME,
        "from_cache": False,
        "last_trade": None,
        "spot": Decimal(100),
        "source": "nasdaq",
        "rows": rows_,
    }
    fields.update(changes)
    return OptionChainResponse.model_validate(fields)


def test_panel_selects_listed_contracts_across_horizon_and_moneyness() -> None:
    calendar = SessionCalendar()
    expiries = [calendar.offset(INPUT, count) for count in (1, 3, 15)]
    quotes = [
        _row("IREN", expiry, strike) for expiry in expiries for strike in (80, 90, 100, 110, 120)
    ]
    chain = _chain("IREN", quotes)
    selected = select_contracts(chain, INPUT, calendar)
    assert len(selected) == 30
    assert all(cell.row is not None and cell.reason is None for cell in selected)
    assert {cell.horizon_band for cell in selected} == {"1", "2-5", "6-25"}
    assert {cell.moneyness for cell in selected} == {
        "near ATM",
        "moderately ITM",
        "moderately OTM",
        "far ITM",
        "far OTM",
    }
    assert all(cell.row.strike == 100 for cell in selected if cell.moneyness == "near ATM")
    assert select_contracts(_chain("IREN", list(reversed(quotes))), INPUT, calendar) == selected


def test_panel_rejects_ambiguous_and_absent_contracts_without_inventing_strikes() -> None:
    calendar = SessionCalendar()
    expiry = calendar.offset(INPUT, 1)
    valid_call = _row("IREN", expiry, 100, put_volume=None, put_open_interest=None)
    adjusted = _row("IREN", expiry, 90, root="ADJ", identity_reason="adjusted")
    duplicate = _row("IREN", expiry, 110)
    chain = _chain("IREN", [valid_call, adjusted, duplicate, duplicate.model_copy()])
    selected = select_contracts(chain, INPUT, calendar)
    assert len(selected) == 30
    assert sum(cell.row is not None for cell in selected) == 1
    assert next(cell for cell in selected if cell.row is not None).side == "call"
    assert any(cell.reason == "no_expiry_in_band" for cell in selected)
    assert all(cell.row is None or cell.row.root == "IREN" for cell in selected)
    assert all(cell.row is None or cell.row.strike == 100 for cell in selected)


def _cohort() -> AuditCohort:
    symbols = [f"A{chr(65 + index // 26)}{chr(65 + index % 26)}" for index in range(AUDIT_SIZE)]
    return AuditCohort(
        completed_session=INPUT,
        frozen_at=datetime(2026, 9, 26, tzinfo=UTC),
        members=tuple(
            AuditMember(ticker, "a" * 64, datetime(2026, 9, 25, 21, tzinfo=UTC), Path("x"))
            for ticker in symbols
        ),
    )


def _issue(ticker: str, expiry: date) -> tuple[ForecastIssuance, PredictiveDistribution]:
    distribution = PredictiveDistribution(
        ticker=ticker,
        status="available",
        reason=None,
        method="lognormal_ewma",
        as_of=INPUT,
        expiry_session=expiry,
        horizon_sessions=1,
        spot=100.0,
        daily_volatility=0.02,
        model_version="panel-test-v1",
        support=60,
        data_hash="b" * 64,
        terminal_prices=(90.0, 100.0, 110.0),
        weights=(0.25, 0.5, 0.25),
    )
    issuance = ForecastIssuance(
        contract_key=make_watch_key(ticker, ticker, "call", expiry.isoformat(), Decimal(100)),
        ticker=ticker,
        root=ticker,
        side="call",
        expiration=expiry,
        expiry_session=expiry,
        strike_exact="100.000",
        terms_note=TERMS_NOTE,
        contract_since=expiry,
        input_session=INPUT,
        input_retrieved_at=datetime(2026, 9, 25, 21, tzinfo=UTC),
        issued_at=SAMPLE_TIME,
        model_version="panel-test-v1",
        method="lognormal_ewma",
        data_hash="b" * 64,
        distribution_hash=None,
        price_basis="completed_close",
        spot_exact="100",
        status="available",
        itm_probability=0.5,
        otm_probability=0.5,
        atm_probability=0.0,
        unavailable_reason=None,
    )
    return issuance, distribution


@pytest.mark.asyncio
async def test_panel_records_selected_and_missing_cells_then_advances_after_restart(
    tmp_path, monkeypatch
) -> None:
    cohort = _cohort()
    monkeypatch.setattr(panel_module, "read_audit_cohort", lambda _data_dir: cohort)
    monkeypatch.setattr(panel_module, "_now", lambda: SAMPLE_TIME)
    expiry = SessionCalendar().offset(INPUT, 1)
    fetched: list[str] = []

    class Service:
        async def get_current_chain(self, ticker: str) -> OptionChainResponse:
            fetched.append(ticker)
            return _chain(
                ticker, [_row(ticker, expiry, 100, put_volume=None, put_open_interest=None)]
            )

    class Predictive:
        forecaster = SimpleNamespace(calendar=SessionCalendar())
        ledger = ForecastLedger(tmp_path)

        async def _refresh(self, _ticker: str) -> None:
            return None

    submitted: list[list[object]] = []
    shadow = SimpleNamespace(submit=lambda entries: submitted.append(entries))
    monkeypatch.setattr(
        panel_module,
        "quant_for_contract",
        lambda _market, _predictive, *, ticker, expiry, **_kwargs: SimpleNamespace(
            issuance=_issue(ticker, expiry)
        ),
    )
    panel = ProspectivePanel(tmp_path, Service(), object(), Predictive(), shadow)
    first = await panel.tick()
    restarted = ProspectivePanel(tmp_path, Service(), object(), Predictive(), shadow)
    second = await restarted.tick()

    assert first != second
    assert fetched == [first, second]
    assert len(submitted) == 2
    with connect(tmp_path / "results.duckdb") as connection:
        cells = rows(connection, "SELECT * FROM forecast_panel_cells ORDER BY ticker, horizon_band")
    assert len(cells) == 60
    assert sum(cell["forecast_status"] == "issued" for cell in cells) == 2
    assert sum(cell["reason"] == "no_expiry_in_band" for cell in cells) == 40
    assert len(ForecastLedger(tmp_path).evaluation_rows()) == 2


@pytest.mark.asyncio
async def test_panel_records_source_outage_as_coverage_rejection(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(panel_module, "read_audit_cohort", lambda _data_dir: _cohort())
    monkeypatch.setattr(panel_module, "_now", lambda: SAMPLE_TIME)

    class BrokenService:
        async def get_current_chain(self, _ticker: str) -> OptionChainResponse:
            raise RuntimeError("provider offline")

    async def refresh(_ticker: str) -> None:
        return None

    predictive = SimpleNamespace(
        forecaster=SimpleNamespace(calendar=SessionCalendar()), _refresh=refresh
    )
    panel = ProspectivePanel(tmp_path, BrokenService(), object(), predictive, object())
    assert await panel.tick() is not None
    with connect(tmp_path / "results.duckdb") as connection:
        cells = rows(connection, "SELECT forecast_status, reason FROM forecast_panel_cells")
    assert len(cells) == 30
    assert all(
        cell == {"forecast_status": "missing", "reason": "chain_source_unavailable"}
        for cell in cells
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"fetched_at": SAMPLE_TIME - timedelta(minutes=5)}, "chain_snapshot_stale"),
        ({"from_cache": True}, "chain_cache_reused"),
    ],
)
async def test_panel_rejects_unfresh_chain_without_issuing(
    tmp_path, monkeypatch, change, reason
) -> None:
    monkeypatch.setattr(panel_module, "read_audit_cohort", lambda _data_dir: _cohort())
    monkeypatch.setattr(panel_module, "_now", lambda: SAMPLE_TIME)
    expiry = SessionCalendar().offset(INPUT, 1)

    class StaleService:
        async def get_current_chain(self, ticker: str) -> OptionChainResponse:
            return _chain(
                ticker,
                [_row(ticker, expiry, 100)],
                **change,
            )

    async def refresh(_ticker: str) -> None:
        return None

    predictive = SimpleNamespace(
        forecaster=SimpleNamespace(calendar=SessionCalendar()), _refresh=refresh
    )
    panel = ProspectivePanel(tmp_path, StaleService(), object(), predictive, object())
    assert await panel.tick() is not None
    with connect(tmp_path / "results.duckdb") as connection:
        cells = rows(connection, "SELECT forecast_status, reason FROM forecast_panel_cells")
    assert len(cells) == 30
    assert all(cell["reason"] == reason for cell in cells)
    assert ForecastLedger(tmp_path).evaluation_rows() == []


@pytest.mark.asyncio
async def test_slow_price_refresh_precedes_fresh_chain_fetch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(panel_module, "read_audit_cohort", lambda _data_dir: _cohort())
    clock = [SAMPLE_TIME]
    monkeypatch.setattr(panel_module, "_now", lambda: clock[0])
    order: list[str] = []

    class Predictive:
        forecaster = SimpleNamespace(calendar=SessionCalendar())

        async def _refresh(self, _ticker: str) -> None:
            order.append("refresh")
            clock[0] += timedelta(minutes=3)

    class Service:
        async def get_current_chain(self, ticker: str) -> OptionChainResponse:
            order.append("chain")
            return _chain(ticker, [], fetched_at=clock[0])

    panel = ProspectivePanel(tmp_path, Service(), object(), Predictive(), object())
    assert await panel.tick() is not None
    assert order == ["refresh", "chain"]
    with connect(tmp_path / "results.duckdb") as connection:
        cells = rows(connection, "SELECT reason FROM forecast_panel_cells")
    assert len(cells) == 30
    assert all(cell["reason"] == "no_standard_contracts" for cell in cells)


def test_panel_rejects_clock_mismatch_before_as_issued_recording(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(panel_module, "_now", lambda: SAMPLE_TIME)
    expiry = SessionCalendar().offset(INPUT, 1)
    good, distribution = _issue("IREN", expiry)
    monkeypatch.setattr(
        panel_module,
        "quant_for_contract",
        lambda *_args, **_kwargs: SimpleNamespace(
            issuance=(replace(good, input_session=INPUT - timedelta(days=1)), distribution)
        ),
    )
    panel = ProspectivePanel(tmp_path, object(), object(), object(), object())
    entries, cells = panel._issue_selected(
        "IREN",
        SAMPLE_TIME,
        INPUT,
        SAMPLE_TIME,
        select_contracts(_chain("IREN", [_row("IREN", expiry, 100)]), INPUT, SessionCalendar()),
    )
    assert entries == []
    assert sum(cell[3] == "issuance_clock_mismatch" for cell in cells) == 2
