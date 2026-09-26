"""Runs, overview, leaderboard, cross-ticker comparison, and strategy detail."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request

from stocksweeper.api.schemas import (
    CrossTickerItem,
    Leaderboard,
    LeaderboardItem,
    OverviewCard,
    RunTickerView,
    RunView,
    StrategyDetail,
)
from stocksweeper.config import TICKER_PATTERN, Settings

if TYPE_CHECKING:
    from stocksweeper.storage.repo import Repository

router = APIRouter(prefix="/api/research")


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _repo(request: Request) -> Repository:
    from stocksweeper.storage.repo import Repository

    return Repository(_settings(request).resolved_data_dir())


def _run_id(request: Request, run_id: str | None) -> str:
    chosen = run_id or _repo(request).latest_run_id()
    if not chosen:
        raise HTTPException(status_code=404, detail="no backtest runs yet")
    if _repo(request).get_run(chosen) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return chosen


@router.get("/runs", response_model=list[RunView])
def runs(request: Request) -> list[RunView]:
    return [RunView.model_validate(row) for row in _repo(request).list_runs()]


@router.get("/runs/{run_id}", response_model=RunView)
def run(run_id: str, request: Request) -> RunView:
    found = _repo(request).get_run(run_id)
    if found is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunView.model_validate(found)


@router.get("/runs/{run_id}/tickers", response_model=list[RunTickerView])
def run_tickers(run_id: str, request: Request) -> list[RunTickerView]:
    if _repo(request).get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [RunTickerView.model_validate(row) for row in _repo(request).run_tickers(run_id)]


@router.get("/overview", response_model=list[OverviewCard])
def overview(request: Request, run_id: str | None = None) -> list[OverviewCard]:
    from stocksweeper.data.store import MarketStore

    settings = _settings(request)
    tickers = list(settings.market.tickers)
    try:
        chosen = _run_id(request, run_id)
    except HTTPException:
        if run_id is not None:
            raise
        chosen = None
    if chosen is not None:
        run_tickers = _repo(request).run_tickers(chosen)
        if run_tickers:
            tickers = [str(item["ticker"]) for item in run_tickers]
    store = MarketStore(settings.resolved_data_dir())
    ticker_status = store.status(
        tickers,
        settings.market.interval,
        settings.market.start_dates,
    )
    status = {item.ticker: item for item in ticker_status}
    cards = []
    overview_rows = _repo(request).overview(chosen, tickers) if chosen is not None else []
    scored = {row["ticker"]: row for row in overview_rows}
    for ticker in tickers:
        item = status[ticker]
        row = scored.get(ticker, {})
        cards.append(
            OverviewCard(
                ticker=ticker,
                bars=item.bars,
                first=item.first,
                last=item.last,
                limited_history=0 < item.bars < settings.validation.limited_history_bars,
                strategy_id=_str(row.get("strategy_id")),
                strategy_name=_str(row.get("strategy_name")),
                signals=_str(row.get("signals")),
                entry_signals=list(row.get("entry_signals") or []),
                filter_signals=list(row.get("filter_signals") or []),
                exit_signals=list(row.get("exit_signals") or []),
                exit_kind=_str(row.get("exit_kind")) or "mirror",
                robustness=_float(row.get("robustness")),
                oos_cagr=_float(row.get("oos_cagr")),
                sharpe=_float(row.get("sharpe")),
                max_drawdown=_float(row.get("max_drawdown")),
                trades=_int(row.get("trades")),
                buy_hold_cagr=_float(row.get("buy_hold_cagr")),
                buy_hold_max_drawdown=_float(row.get("buy_hold_max_drawdown")),
                test_cagr=_float(row.get("test_cagr")),
                test_max_drawdown=_float(row.get("test_max_drawdown")),
                test_trades=_int(row.get("test_trades")),
                test_buy_hold_cagr=_float(row.get("test_buy_hold_cagr")),
            )
        )
    return cards


@router.get("/leaderboard", response_model=Leaderboard)
def leaderboard(
    request: Request,
    run_id: str | None = None,
    ticker: str | None = None,
    family: str | None = None,
    min_trades: int | None = Query(default=None, ge=0),
    include_rejected: bool = False,
    sort: str = "robustness",
    order: str = "desc",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    group_variants: bool = False,
    rule: str | None = None,
    exit_kind: str | None = None,
    filters: int | None = Query(default=None, ge=0, le=2),
) -> Leaderboard:
    chosen = _run_id(request, run_id)
    payload = _repo(request).leaderboard(
        chosen,
        ticker=ticker,
        family=family,
        min_trades=min_trades,
        include_rejected=include_rejected,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
        group_variants=group_variants,
        rule=rule,
        exit_kind=exit_kind if exit_kind in {"mirror", "atr_trail", "time"} else None,
        filter_count=filters,
    )
    return Leaderboard(
        run_id=chosen,
        total=payload["total"],
        families=payload["families"],
        items=[LeaderboardItem.model_validate(item) for item in payload["items"]],
    )


@router.get("/cross-ticker", response_model=list[CrossTickerItem])
def cross_ticker(request: Request, run_id: str | None = None) -> list[CrossTickerItem]:
    chosen = _run_id(request, run_id)
    return [CrossTickerItem.model_validate(item) for item in _repo(request).cross_ticker(chosen)]


@router.get("/strategies/{strategy_id}", response_model=StrategyDetail)
def strategy_detail(
    strategy_id: str,
    request: Request,
    ticker: str = Query(pattern=TICKER_PATTERN),
    run_id: str | None = None,
) -> StrategyDetail:
    from stocksweeper.pipeline.detail import RunSnapshotMissing, load_detail

    chosen = _run_id(request, run_id)
    try:
        payload = load_detail(_settings(request), chosen, strategy_id, ticker)
    except RunSnapshotMissing as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="strategy not found for this run and ticker")
    return StrategyDetail.model_validate(payload)


def _str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


def _int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)  # type: ignore[arg-type]
