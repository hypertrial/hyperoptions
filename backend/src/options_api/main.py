from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from options_api.cache import TickerCache
from options_api.chain import load_cash_secured_puts, load_covered_calls
from options_api.models import (
    CashSecuredPutPage,
    CoveredCallPage,
    HealthResponse,
    Moneyness,
    TickerSearchResponse,
    normalize_ticker,
)
from options_api.nasdaq import NasdaqError, create_http_client
from options_api.outcomes import CloseProvider
from options_api.service import OptionChainService
from options_api.universe import TickerUniverse
from options_api.watchlist import WatchlistService, router as watchlist_router
from stocksweeper.api.routes_data import jobs_router, router as research_data_router
from stocksweeper.api.routes_meta import router as research_meta_router
from stocksweeper.api.routes_results import router as research_results_router
from stocksweeper.config import Settings, load_settings
from stocksweeper.pipeline.jobs import JobManager
from stocksweeper.storage.db import single_instance

ALLOWED_ORIGIN = "http://localhost:5173"
ALLOWED_ORIGINS = {ALLOWED_ORIGIN, "http://127.0.0.1:5173"}
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
MAX_WRITE_BODY = 16 * 1024
LOG = logging.getLogger(__name__)


def _allowed_host(value: str) -> bool:
    if value == "::1":
        return True
    if (
        not value
        or value.endswith(":")
        or any(char.isspace() for char in value)
        or any(char in value for char in "/?#")
    ):
        return False
    try:
        authority = urlsplit(f"//{value}")
        # Accessing port raises ValueError for an invalid port.
        _ = authority.port
    except ValueError:
        return False
    return (
        authority.hostname in ALLOWED_HOSTS
        and authority.username is None
        and authority.password is None
        and not authority.path
        and not authority.query
        and not authority.fragment
    )


class LocalHostMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            headers = Headers(scope=scope)
            hosts = headers.getlist("host")
            if len(hosts) != 1 or not _allowed_host(hosts[0]):
                response = PlainTextResponse("Invalid host header", status_code=400)
                await response(scope, receive, send)
                return
            if scope["type"] == "http" and scope.get("path", "").startswith("/api/"):
                fetch_sites = headers.getlist("sec-fetch-site")
                if len(fetch_sites) > 1 or fetch_sites == ["cross-site"]:
                    response = PlainTextResponse("Cross-site request blocked", status_code=403)
                    await response(scope, receive, send)
                    return
            if (
                scope["type"] == "http"
                and scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}
                and scope.get("path", "").startswith("/api/")
            ):
                origins = headers.getlist("origin")
                if len(origins) != 1 or origins[0] not in ALLOWED_ORIGINS:
                    response = PlainTextResponse("Origin not allowed", status_code=403)
                    await response(scope, receive, send)
                    return
                if scope["method"] != "DELETE":
                    media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if media_type != "application/json":
                        response = PlainTextResponse("JSON request required", status_code=415)
                        await response(scope, receive, send)
                        return
                lengths = headers.getlist("content-length")
                try:
                    length = int(lengths[0]) if len(lengths) == 1 else 0
                except ValueError:
                    length = -1
                if len(lengths) > 1 or length < 0:
                    response = PlainTextResponse("Invalid content length", status_code=400)
                    await response(scope, receive, send)
                    return
                if length > MAX_WRITE_BODY:
                    response = PlainTextResponse("Request too large", status_code=413)
                    await response(scope, receive, send)
                    return

                total = 0
                buffered: list[dict[str, object]] = []
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        continue
                    total += len(message.get("body", b""))
                    if total > MAX_WRITE_BODY:
                        response = PlainTextResponse("Request too large", status_code=413)
                        await response(scope, receive, send)
                        return
                    buffered.append(message)
                    if not message.get("more_body", False):
                        break

                async def replay_receive() -> dict[str, object]:
                    return buffered.pop(0) if buffered else await receive()

                await self.app(scope, replay_receive, send)
                return
        await self.app(scope, receive, send)


router = APIRouter()


@router.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(ok=True)


def _http_nasdaq_error(exc: NasdaqError) -> HTTPException:
    headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
    return HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers)


def _page_now(app: FastAPI) -> datetime:
    configured = app.state.clock()
    if not isinstance(configured, datetime):
        configured = datetime.now(UTC)
    return configured if configured.tzinfo is not None else configured.replace(tzinfo=UTC)


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="Origin not allowed")


async def _known_ticker(request: Request, ticker: str) -> str:
    normalized = normalize_ticker(ticker)
    if normalized is None:
        raise HTTPException(status_code=400, detail="Invalid ticker")
    universe: TickerUniverse | None = getattr(request.app.state, "universe", None)
    if universe is None:
        raise HTTPException(status_code=503, detail="Ticker universe unavailable")
    await universe.ensure()
    if not universe.available:
        raise HTTPException(status_code=503, detail="Ticker universe unavailable")
    if not universe.contains(normalized):
        raise HTTPException(status_code=404, detail="Unknown Nasdaq ticker")
    return normalized


@router.get("/api/tickers", response_model=TickerSearchResponse)
async def search_tickers(
    request: Request,
    q: Annotated[str, Query(max_length=32)] = "",
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> TickerSearchResponse:
    _check_origin(request)
    universe: TickerUniverse | None = getattr(request.app.state, "universe", None)
    if universe is None:
        raise HTTPException(status_code=503, detail="Ticker universe unavailable")
    await universe.ensure()
    if not universe.available:
        raise HTTPException(status_code=503, detail="Ticker universe unavailable")
    as_of = universe.as_of or _page_now(request.app)
    return TickerSearchResponse(
        as_of=as_of,
        total=len(universe.listings),
        results=universe.search(q, limit),
    )


async def _load_page(
    request: Request,
    ticker: str,
    load: Callable[..., Awaitable[CoveredCallPage | CashSecuredPutPage]],
    moneyness: Moneyness | None,
) -> CoveredCallPage | CashSecuredPutPage:
    _check_origin(request)
    normalized = await _known_ticker(request, ticker)
    universe: TickerUniverse = request.app.state.universe
    listing = universe.listing(normalized)
    service: OptionChainService = request.app.state.service
    try:
        return await load(
            service,
            normalized,
            _page_now(request.app),
            moneyness=moneyness,
            name=listing.name if listing else None,
        )
    except NasdaqError as exc:
        raise _http_nasdaq_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Nasdaq response is malformed") from exc


@router.get("/api/covered-calls/{ticker}", response_model=CoveredCallPage)
async def get_covered_calls(
    request: Request,
    ticker: Annotated[str, Path(min_length=1, max_length=8)],
    moneyness: Annotated[Moneyness | None, Query()] = None,
) -> CoveredCallPage:
    return await _load_page(request, ticker, load_covered_calls, moneyness)


@router.get("/api/cash-secured-puts/{ticker}", response_model=CashSecuredPutPage)
async def get_cash_secured_puts(
    request: Request,
    ticker: Annotated[str, Path(min_length=1, max_length=8)],
    moneyness: Annotated[Moneyness | None, Query()] = None,
) -> CashSecuredPutPage:
    return await _load_page(request, ticker, load_cash_secured_puts, moneyness)


def create_app(
    *,
    client_factory: Callable[[], httpx.AsyncClient] = create_http_client,
    clock: Callable[[], datetime] | None = None,
    prefetch_universe: bool = True,
    research_settings: Settings | None = None,
    close_provider: CloseProvider | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = research_settings or load_settings()
        with single_instance(settings.resolved_data_dir()) as research_lock:
            app.state.settings = settings
            app.state.jobs = JobManager(settings.resolved_data_dir())
            app.state.watchlist = WatchlistService(settings.resolved_data_dir(), close_provider)
            from stocksweeper.forecast import ForecastService, PeerCandidate

            app.state.forecast = ForecastService(settings.resolved_data_dir())
            app.state.forecast.initialize()
            app.state.watchlist.forecast = app.state.forecast
            client = client_factory()
            app.state.http_client = client
            app.state.service = OptionChainService(
                client=client,
                cache=TickerCache(ttl_seconds=30.0, max_entries=64),
                info_cache=TickerCache(ttl_seconds=30.0, max_entries=64),
                history_cache=TickerCache(ttl_seconds=86_400.0, max_entries=64),
            )
            app.state.universe = TickerUniverse(client)
            app.state.clock = clock or (lambda: datetime.now(UTC))
            app.state.prefetch_universe = prefetch_universe
            prefetch: asyncio.Task[bool] | None = None
            if prefetch_universe:
                prefetch = asyncio.create_task(app.state.universe.ensure())

            async def refresh_watches() -> None:
                first = True
                while True:
                    try:
                        await app.state.universe.ensure()
                        if app.state.universe.available:
                            candidates = [
                                PeerCandidate(ticker=listing.symbol, sector=listing.sector)
                                for listing in app.state.universe.listings
                            ]
                            app.state.watchlist.queue_refresh(
                                app.state.jobs,
                                as_of=_page_now(app),
                                candidates=candidates,
                                retry_pending=first,
                                clock=app.state.clock,
                            )
                            first = False
                    except Exception:
                        # A provider outage must not stop subsequent daily checks.
                        LOG.exception("watch refresh scheduler failed")
                    await asyncio.sleep(300)

            watch_poll = asyncio.create_task(refresh_watches()) if prefetch_universe else None
            try:
                yield
            finally:
                if watch_poll is not None:
                    watch_poll.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watch_poll
                if prefetch is not None and not prefetch.done():
                    prefetch.cancel()
                app.state.jobs.stop_accepting()
                drained = await asyncio.to_thread(app.state.jobs.wait, 2.0)
                if not drained:
                    research_lock.defer_until(app.state.jobs.wait)
                await app.state.universe.close()
                if prefetch is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await prefetch
                await client.aclose()

    app = FastAPI(title="Nasdaq option chain", lifespan=lifespan)
    app.state.allowed_origins = ALLOWED_ORIGINS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(ALLOWED_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )
    app.add_middleware(LocalHostMiddleware)
    app.include_router(router)
    app.include_router(watchlist_router, dependencies=[Depends(_check_origin)])
    for research_router in (research_meta_router, research_data_router, research_results_router):
        app.include_router(research_router, dependencies=[Depends(_check_origin)])
    app.include_router(jobs_router, dependencies=[Depends(_check_origin)])
    return app


app = create_app()
