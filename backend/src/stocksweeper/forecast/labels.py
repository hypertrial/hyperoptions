"""Collect point-in-time expiry labels for already issued contract forecasts."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from options_api.models import HistoricalResponse
from options_api.outcomes import (
    MAX_OUTCOME_HISTORY_DAYS,
    TERMS_NOTE,
    CloseProvider,
    ForecastLabelResolution,
    YahooCloseProvider,
    resolve_forecast_label,
)
from stocksweeper.forecast.ledger import ForecastLabel, ForecastLedger

_NY = ZoneInfo("America/New_York")


class NasdaqHistoryProvider(Protocol):
    async def get_history(self, ticker: str, from_date: str) -> HistoricalResponse: ...


async def collect_matured_labels(
    ledger: ForecastLedger,
    nasdaq: NasdaqHistoryProvider,
    *,
    as_of: datetime,
    yahoo: CloseProvider | None = None,
    limit: int = 50,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, int]:
    """Append every due source check; a revised close never rewrites an issuance."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must have a timezone")
    yahoo = yahoo or YahooCloseProvider()
    clock = clock or (lambda: datetime.now(UTC))
    outcomes: Counter[str] = Counter()
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for contract in ledger.due_label_contracts(as_of, limit=limit):
        groups[str(contract["ticker"])].append(contract)
    today = as_of.astimezone(_NY).date()
    end = today + timedelta(days=1)
    for ticker, contracts in groups.items():
        eligible = [
            contract
            for contract in contracts
            if contract["root"] == ticker
            and contract["terms_note"] == TERMS_NOTE
            and contract["contract_since"] is not None
            and contract["contract_since"] <= contract["expiry_session"]
            and (today - contract["contract_since"]).days <= MAX_OUTCOME_HISTORY_DAYS
        ]
        nasdaq_closes = {}
        ambiguous_sessions = set()
        yahoo_history = None
        nasdaq_failed = False
        yahoo_failed = False
        if eligible:
            try:
                response = await nasdaq.get_history(
                    ticker, min(contract["expiry_session"] for contract in eligible).isoformat()
                )
                for bar in response.bars:
                    previous = nasdaq_closes.setdefault(bar.date, bar.close)
                    if previous != bar.close:
                        ambiguous_sessions.add(bar.date)
            except Exception:
                nasdaq_failed = True
            try:
                yahoo_history = await asyncio.to_thread(
                    yahoo.fetch,
                    ticker,
                    min(contract["contract_since"] for contract in eligible),
                    end,
                )
            except Exception:
                yahoo_failed = True
        for contract in contracts:
            target = contract["expiry_session"]
            nasdaq_close = nasdaq_closes.get(target)
            resolution = resolve_forecast_label(
                ticker=ticker,
                root=str(contract["root"]),
                side=contract["side"],
                strike=Decimal(str(contract["strike_exact"])),
                expiration=contract["expiration"],
                contract_since=contract["contract_since"],
                as_of=as_of,
                nasdaq_close=nasdaq_close,
                yahoo_history=yahoo_history,
                standard_terms=contract["terms_note"] == TERMS_NOTE,
            )
            if target in ambiguous_sessions:
                resolution = ForecastLabelResolution(
                    "excluded",
                    "nasdaq_close_ambiguous",
                    None,
                    target,
                    None,
                    None,
                    None,
                    None,
                )
            if resolution.status == "pending" and (nasdaq_failed or yahoo_failed):
                reason = (
                    "nasdaq_source_unavailable" if nasdaq_failed else "yahoo_source_unavailable"
                )
                resolution = ForecastLabelResolution(
                    "pending", reason, None, target, nasdaq_close, None, None, None
                )
            ledger.record_label(
                ForecastLabel(
                    contract_key=str(contract["contract_key"]),
                    terms_note=str(contract["terms_note"]),
                    expiry_session=target,
                    checked_at=clock(),
                    status=resolution.status,
                    reason=resolution.reason,
                    source=resolution.source,
                    nasdaq_close_exact=str(resolution.nasdaq_close)
                    if resolution.nasdaq_close is not None
                    else None,
                    yahoo_close_exact=str(resolution.yahoo_close)
                    if resolution.yahoo_close is not None
                    else None,
                    selected_close_exact=str(resolution.selected_close)
                    if resolution.selected_close is not None
                    else None,
                    classification=resolution.classification,
                )
            )
            outcomes[resolution.reason or "valid"] += 1
    return dict(outcomes)
