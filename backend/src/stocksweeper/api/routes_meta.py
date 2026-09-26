"""Health and configuration."""

from __future__ import annotations

from fastapi import APIRouter, Request

from stocksweeper.api.schemas import ConfigView
from stocksweeper.config import Settings

router = APIRouter(prefix="/api/research")


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


@router.get("/config", response_model=ConfigView)
def config(request: Request) -> ConfigView:
    settings = _settings(request)
    return ConfigView(
        tickers=list(settings.market.tickers),
        interval=settings.market.interval,
        initial_capital=settings.backtest.initial_capital,
        fees=settings.backtest.fees,
        slippage=settings.backtest.slippage,
        max_strategies=settings.generator.max_strategies,
        gates=settings.gates.model_dump(),
        robustness_weights=settings.robustness.model_dump(),
        cross_ticker_min=settings.cross_ticker.min_tickers,
    )
