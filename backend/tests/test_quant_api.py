"""The chain API keeps quote odds and physical forecasts separate."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from options_api.chain import assemble_covered_calls
from options_api.main import create_app
from options_api.market_watch import EntryQuote
from options_api.models import MarketOddsView, PredictiveOddsView, TickerListing
from stocksweeper.config import Settings
from stocksweeper.forecast.predictive import PredictiveDistribution

from .synthetic import synthetic_context


def test_chain_serializes_predictive_fallback_and_coherent_payoff(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain, info, history, today, now = synthetic_context()
    quoted = chain.rows[4].model_copy(update={"root": "IREN"})
    chain.rows[4] = quoted
    page = assemble_covered_calls(chain, info, history, today, now, "all")

    async def load(*_args, **_kwargs):
        return page

    monkeypatch.setattr("options_api.main.load_covered_calls", load)
    app = create_app(
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(503))
        ),
        clock=lambda: now,
        prefetch_universe=False,
        research_settings=Settings(data_dir=tmp_path),
        predictive_refresh=False,
    )
    distribution = PredictiveDistribution(
        ticker="IREN", status="available", reason=None, method="empirical_scaled",
        as_of=date(2026, 9, 10), expiry_session=date.fromisoformat(quoted.expiration),
        horizon_sessions=11, spot=50.0, daily_volatility=0.02, model_version="test-v1",
        support=3, data_hash="abc123", terminal_prices=(45.0, 50.0, 55.0),
        weights=(0.2, 0.5, 0.3),
    )
    quote = EntryQuote(
        spot=Decimal("50"), stock_ask=Decimal("50.10"),
        bid=quoted.call_bid or Decimal(0), ask=quoted.call_ask or Decimal(0),
        session_date=today, source="nasdaq", fetched_at=chain.fetched_at,
        rate=Decimal("0.04"), valuation_time=now,
        rate_as_of_session=today,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.universe.seed([TickerListing(symbol="IREN", name="IREN")])
        monkeypatch.setattr(app.state.market_odds, "schedule", lambda _tickers: None)
        monkeypatch.setattr(
            app.state.market_odds, "schedule_for_chain", lambda *_args: True
        )
        monkeypatch.setattr(
            app.state.market_odds,
            "lookup",
            lambda *_identity: MarketOddsView(
                status="unavailable",
                reason="Reliable call quotes do not bracket this strike",
                source="nasdaq", session_date=today,
            ),
        )
        monkeypatch.setattr(app.state.market_odds, "entry_quote", lambda *_identity: quote)
        monkeypatch.setattr(app.state.predictive_odds, "schedule", lambda _tickers: None)
        monkeypatch.setattr(
            app.state.predictive_odds, "cache_retrieved_at", lambda *_args: now
        )
        monkeypatch.setattr(
            app.state.predictive_odds,
            "lookup",
            lambda *_args, **_kwargs: (
                PredictiveOddsView(
                    status="available", method="empirical_scaled", itm_pct_tenths=300,
                    otm_pct_tenths=700, atm_pct_tenths=0,
                    as_of_session=distribution.as_of,
                    expiry_session=distribution.expiry_session,
                    model_version="test-v1", support=3, data_hash="abc123",
                    price_basis="completed_close",
                ),
                distribution,
            ),
        )
        response = client.get(
            "/api/covered-calls/IREN?moneyness=all&forecast_model=empirical_scaled"
        )
        default_response = client.get("/api/covered-calls/IREN?moneyness=all")
        invalid_response = client.get("/api/covered-calls/IREN?forecast_model=unknown")
        page = assemble_covered_calls(chain, info, history, today, now, "all")

        def failed_evidence(_entries):
            raise OSError("evidence disk unavailable")

        monkeypatch.setattr(app.state.predictive_odds.ledger, "record_batch", failed_evidence)
        degraded = client.get(
            "/api/covered-calls/IREN?moneyness=all&forecast_model=empirical_scaled"
        )
        selected_unavailable = client.get(
            "/api/covered-calls/IREN?moneyness=all&forecast_model=intraday_shadow"
        )

    assert response.status_code == 200
    body = response.json()
    row = next(
        contract
        for group in body["expirations"]
        for contract in group["contracts"]
        if contract["watch_key"] is not None
    )
    assert row["market_odds"]["status"] == "unavailable"
    assert row["predictive_odds"]["status"] == "available"
    assert row["predictive_odds"]["method"] == "empirical_scaled"
    assert [view["method"] for view in row["physical_models"]] == [
        "lognormal_ewma", "empirical_scaled", "student_t_ewma",
        "gjr_garch_t", "intraday_shadow",
    ]
    assert [view["method"] for view in row["market_models"]] == [
        "regimelib", "constrained_call_curve",
    ]
    assert row["hypothetical_risk"]["forecast_method"] == "empirical_scaled"
    assert row["predictive_odds"]["model_version"] == "test-v1"
    assert row["hypothetical_risk"]["status"] == "available"
    assert row["hypothetical_risk"]["assumed_spot_cents"] == 5010
    assert row["hypothetical_risk"]["assumed_bid_cents"] == int(quoted.call_bid * 100)
    assert row["hypothetical_risk"]["quote_session"] == today.isoformat()
    assert body["chain_fetched_at"] == now.isoformat().replace("+00:00", "Z")
    default_row = next(
        contract for group in default_response.json()["expirations"]
        for contract in group["contracts"] if contract["watch_key"] is not None
    )
    assert default_row["predictive_odds"]["method"] == "lognormal_ewma"
    assert default_row["predictive_odds"]["status"] == "pending"
    assert default_row["hypothetical_risk"]["status"] == "unavailable"
    assert invalid_response.status_code == 422
    assert degraded.status_code == 200
    degraded_row = next(
        contract
        for group in degraded.json()["expirations"]
        for contract in group["contracts"]
        if contract["watch_key"] is not None
    )
    assert degraded_row["market_odds"]["status"] == "unavailable"
    assert degraded_row["predictive_odds"]["status"] == "unavailable"
    assert degraded_row["predictive_odds"]["reason"] == "Forecast evidence unavailable"
    assert degraded_row["hypothetical_risk"]["status"] == "unavailable"
    unavailable_row = next(
        contract for group in selected_unavailable.json()["expirations"]
        for contract in group["contracts"] if contract["watch_key"] is not None
    )
    assert unavailable_row["predictive_odds"]["status"] == "unavailable"
    assert all(
        view["status"] == "unavailable" for view in unavailable_row["physical_models"]
    )
