"""Data updates, sweeps, and job status."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from stocksweeper.api.schemas import BacktestRequest, JobView, TickerStatusView, UpdateRequest
from stocksweeper.config import Settings
from stocksweeper.pipeline.jobs import JobBusy, JobManager

router = APIRouter(prefix="/api")
jobs_router = APIRouter(prefix="/api")


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _jobs(request: Request) -> JobManager:
    return request.app.state.jobs  # type: ignore[no-any-return]


@router.get("/research/data/status", response_model=list[TickerStatusView])
def data_status(request: Request) -> list[TickerStatusView]:
    from stocksweeper.data.store import MarketStore

    settings = _settings(request)
    store = MarketStore(settings.resolved_data_dir())
    views: list[TickerStatusView] = []
    for item in store.status(
        list(settings.market.tickers),
        settings.market.interval,
        settings.market.start_dates,
    ):
        views.append(
            TickerStatusView(
                ticker=item.ticker,
                bars=item.bars,
                first=item.first,
                last=item.last,
                has_indicators=item.has_indicators,
                limited_history=0 < item.bars < settings.validation.limited_history_bars,
            )
        )
    return views


def _require_local_json(request: Request) -> None:
    if request.headers.get("origin") not in request.app.state.allowed_origins:
        raise HTTPException(status_code=403, detail="Origin required")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="JSON request required")


@router.post(
    "/research/data/update",
    response_model=JobView,
    dependencies=[Depends(_require_local_json)],
)
async def update_data(body: UpdateRequest, request: Request) -> JobView:
    settings = _settings(request)
    chosen = await _validated_tickers(request, body.tickers or list(settings.market.tickers))
    provider = getattr(request.app.state, "provider", None)

    def worker(progress: object) -> None:
        from stocksweeper.pipeline.update import update_market_data

        update_market_data(
            settings,
            full_refresh=body.full_refresh,
            tickers=chosen,
            provider=provider,
            progress=progress,  # type: ignore[arg-type]
        )
        return None

    return _submit(request, "data", worker)


@router.post(
    "/research/backtest/run",
    response_model=JobView,
    dependencies=[Depends(_require_local_json)],
)
async def start_backtest(body: BacktestRequest, request: Request) -> JobView:
    chosen = await _validated_tickers(
        request, body.tickers or list(_settings(request).market.tickers)
    )
    settings = _apply(request, body, chosen)

    def worker(progress: object) -> str:
        from stocksweeper.pipeline.sweep import run_sweep

        return run_sweep(settings, progress)  # type: ignore[arg-type]

    return _submit(request, "backtest", worker)


@jobs_router.get("/jobs/{job_id}", response_model=JobView)
def job(job_id: str, request: Request) -> JobView:
    found = _jobs(request).get(job_id)
    if found is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobView(**found.model_dump())


def _submit(request: Request, kind: str, worker: object) -> JobView:
    try:
        job = _jobs(request).submit(kind, worker)  # type: ignore[arg-type]
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobView(**job.model_dump())


def _apply(request: Request, body: BacktestRequest, tickers: list[str]) -> Settings:
    settings = _settings(request)
    updates: dict[str, object] = {
        "market": settings.market.model_copy(update={"tickers": tickers})
    }
    if body.max_strategies is not None:
        generator = settings.generator.model_copy(update={"max_strategies": body.max_strategies})
        updates["generator"] = generator
    return settings.model_copy(update=updates)  # type: ignore[arg-type]


async def _validated_tickers(request: Request, tickers: list[str]) -> list[str]:
    unique = list(dict.fromkeys(tickers))
    if not unique or len(unique) > 20:
        raise HTTPException(status_code=422, detail="choose between 1 and 20 tickers")
    universe = getattr(request.app.state, "universe", None)
    if universe is None:
        raise HTTPException(status_code=503, detail="Ticker universe unavailable")
    await universe.ensure()
    if not universe.available:
        raise HTTPException(status_code=503, detail="Ticker universe unavailable")
    missing = [ticker for ticker in unique if not universe.contains(ticker)]
    if missing:
        raise HTTPException(status_code=404, detail="Unknown Nasdaq ticker: " + ", ".join(missing))
    return unique
