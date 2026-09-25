from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Path, Query, Request
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
from options_api.service import OptionChainService
from options_api.universe import TickerUniverse

ALLOWED_ORIGIN = "http://localhost:5173"
ALLOWED_ORIGINS = {ALLOWED_ORIGIN, "http://127.0.0.1:5173"}
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


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
            hosts = Headers(scope=scope).getlist("host")
            if len(hosts) != 1 or not _allowed_host(hosts[0]):
                response = PlainTextResponse("Invalid host header", status_code=400)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


router = APIRouter()


@router.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(ok=True)


def _http_nasdaq_error(exc: NasdaqError) -> HTTPException:
    headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
    return HTTPException(
        status_code=exc.status_code, detail=exc.detail, headers=headers
    )


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
        raise HTTPException(
            status_code=502, detail="Nasdaq response is malformed"
        ) from exc


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
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="Nasdaq option chain", lifespan=lifespan)
    app.add_middleware(LocalHostMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(ALLOWED_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
