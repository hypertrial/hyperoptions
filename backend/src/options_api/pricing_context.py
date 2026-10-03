"""Entry quotes for live Greeks, shared by the chain page and the odds refresh.

The page prices the chain it is already showing. The refresh publishes the same
quote after its fit, so the two paths cannot drift.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from options_api.market_calendar import quote_session, regular_session_open, session_close
from options_api.market_sources import DividendStatus, TreasuryCurve
from options_api.models import (
    HistoricalBar,
    IvReason,
    IvReasonCode,
    IvSpotBasis,
    OptionChainResponse,
    OptionQuote,
    StockInfoResponse,
)

_NY = ZoneInfo("America/New_York")
_DEFAULT_QUOTE_MAX_AGE = timedelta(minutes=5)


@dataclass(frozen=True)
class PricingIssue:
    code: IvReasonCode
    message: str
    fallback_allowed: bool = False

    def view(self) -> IvReason:
        return IvReason(code=self.code, message=self.message)


def pricing_issue(reason: str) -> PricingIssue:
    """Map internal fixed reasons; never expose unexpected provider/exception text."""
    messages: dict[str, IvReasonCode] = {
        "Option chain belongs to another quote session": "chain_session",
        "Option-chain coverage is incomplete": "chain_session",
        "Official completed-session close required": "underlying",
        "Official completed-session close is unavailable": "underlying",
        "A coherent underlying bid and ask is unavailable": "underlying",
        "Underlying quote is not marked real time": "underlying",
        "Underlying quote time or price is unavailable": "underlying",
        "Underlying quote is stale": "underlying",
        "Same-day option quote timing cannot be verified": "expiry_timing",
        "Dated Treasury rate is unavailable": "treasury",
        "Dividend exposure is unsupported for this expiration": "dividends",
        "Dividend status could not be verified": "dividends",
        "Dividend exposure is unsupported": "dividends",
        "Contract terms cannot be verified": "contract_identity",
        "Contract appears more than once in the chain": "contract_identity",
        "Option bid or ask is missing or non-finite": "option_quote",
        "Option quote is invalid or crossed": "option_quote",
        "Locked option quotes do not support live IV": "option_quote",
        "Option ask exceeds the live pricing cap": "option_quote",
        "Option quote has insufficient trading activity": "option_quote",
        "Option bid/ask spread is too wide": "option_quote",
    }
    code = messages.get(reason)
    return (
        PricingIssue(code, reason)
        if code is not None
        else PricingIssue("numerical_failure", "Pricing inputs could not be validated")
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _underlying_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%b %d, %Y %I:%M %p ET").replace(tzinfo=_NY).astimezone(UTC)
    except ValueError:
        return None


def _spot(
    chain: OptionChainResponse,
    info: StockInfoResponse,
    now: datetime,
    quote_max_age: timedelta,
) -> tuple[Decimal | None, datetime | None, str | None]:
    if not regular_session_open(now):
        return None, None, "Official completed-session close required"
    if chain.source == "yahoo":
        price = chain.spot
        quote_time = _underlying_time(chain.last_trade_timestamp)
    else:
        bid, ask = info.bid, info.ask
        if (
            bid is None
            or ask is None
            or not bid.is_finite()
            or not ask.is_finite()
            or bid <= 0
            or ask < bid
            or (ask - bid) / ((bid + ask) / 2) > Decimal("0.02")
            or abs((chain.fetched_at - info.fetched_at).total_seconds()) > 120
        ):
            return None, None, "A coherent underlying bid and ask is unavailable"
        price = (bid + ask) / 2
        quote_time = _underlying_time(info.quote_timestamp)
        if not info.is_real_time:
            return None, None, "Underlying quote is not marked real time"
    if price is None or not price.is_finite() or price <= 0 or quote_time is None:
        return None, None, "Underlying quote time or price is unavailable"
    age = (now - quote_time).total_seconds()
    if age < -60 or age > quote_max_age.total_seconds():
        return None, None, "Underlying quote is stale"
    return price, quote_time, None


def positive_close(
    bars: list[HistoricalBar] | tuple[HistoricalBar, ...], session: date
) -> Decimal | None:
    for bar in bars:
        if bar.date == session and bar.close.is_finite() and bar.close > 0:
            return bar.close
    return None


def _entry_bid_ask(
    row: OptionQuote, side: str, spot: Decimal
) -> tuple[tuple[Decimal, Decimal] | None, PricingIssue | None]:
    bid = row.call_bid if side == "call" else row.put_bid
    ask = row.call_ask if side == "call" else row.put_ask
    interest = row.call_open_interest if side == "call" else row.put_open_interest
    volume = row.call_volume if side == "call" else row.put_volume
    if bid is None or ask is None or not (bid.is_finite() and ask.is_finite()):
        return None, pricing_issue("Option bid or ask is missing or non-finite")
    if bid < 0 or ask < bid:
        return None, pricing_issue("Option quote is invalid or crossed")
    if ask == bid:
        return None, pricing_issue("Locked option quotes do not support live IV")
    if ask >= (spot if side == "call" else row.strike):
        return None, pricing_issue("Option ask exceeds the live pricing cap")
    if (interest or 0) < 5 and (volume or 0) < 5:
        return None, pricing_issue("Option quote has insufficient trading activity")
    if ask - bid > max(Decimal("0.25"), (bid + ask) / 2 * Decimal("0.25")):
        return None, pricing_issue("Option bid/ask spread is too wide")
    return (bid, ask), None


@dataclass(frozen=True)
class EntryQuote:
    spot: Decimal
    stock_ask: Decimal | None
    bid: Decimal
    ask: Decimal
    session_date: date
    source: str
    fetched_at: datetime
    rate: Decimal | None
    valuation_time: datetime
    rate_as_of_session: date | None
    spot_basis: IvSpotBasis | None = None
    underlying_quote_time: datetime | None = None


@dataclass(frozen=True)
class PricingSelection:
    quote: EntryQuote | None
    issue: PricingIssue | None = None


@dataclass(frozen=True)
class PricingContext:
    spot: Decimal
    stock_ask: Decimal | None
    session_date: date
    source: str
    fetched_at: datetime
    valuation_time: datetime
    underlying_quote_time: datetime | None
    underlying_quote_fetched_at: datetime | None
    rate_as_of_session: date | None
    rates: dict[str, Decimal]
    reasons: dict[str, str]
    entry_quotes: dict[tuple[str, str, Decimal], tuple[Decimal, Decimal]]
    valid_contracts: set[tuple[str, Decimal]]
    invalid_contracts: set[tuple[str, Decimal]]
    spot_basis: IvSpotBasis | None = None
    quote_issues: dict[tuple[str, str, Decimal], PricingIssue] = field(default_factory=dict)
    identity_issues: dict[tuple[str, Decimal], PricingIssue] = field(default_factory=dict)
    acquisition_expirations: set[str] = field(default_factory=set)

    def entry_quote(self, side: str, expiry: str, strike: Decimal) -> EntryQuote | None:
        if (expiry, strike) not in self.valid_contracts:
            return None
        quote = self.entry_quotes.get((side, expiry, strike))
        if quote is None:
            return None
        return EntryQuote(
            self.spot,
            self.stock_ask,
            quote[0],
            quote[1],
            self.session_date,
            self.source,
            self.fetched_at,
            self.rates.get(expiry),
            self.valuation_time,
            self.rate_as_of_session,
            self.spot_basis,
            self.underlying_quote_time,
        )

    def selection(self, side: str, expiry: str, strike: Decimal) -> PricingSelection:
        issue = self.identity_issues.get((expiry, strike))
        if issue is not None or (expiry, strike) not in self.valid_contracts:
            return PricingSelection(
                None, issue or pricing_issue("Contract terms cannot be verified")
            )
        quote = self.entry_quote(side, expiry, strike)
        if expiry in self.acquisition_expirations and quote is not None:
            return PricingSelection(quote, PricingIssue(
                "input_acquisition", "Treasury or dividend inputs could not be acquired", True
            ))
        if expiry in self.acquisition_expirations:
            return PricingSelection(None, self.quote_issues.get((side, expiry, strike)))
        if expiry in self.reasons:
            return PricingSelection(quote, pricing_issue(self.reasons[expiry]))
        return PricingSelection(quote, self.quote_issues.get((side, expiry, strike)))


def displayed_chain_reason(chain: OptionChainResponse, now: datetime) -> str | None:
    now = _as_utc(now)
    if quote_session(chain.fetched_at) != quote_session(now):
        return "Option chain belongs to another quote session"
    if chain.truncated:
        return "Option-chain coverage is incomplete"
    return None


def pricing_block_reason(
    chain: OptionChainResponse,
    info: StockInfoResponse,
    now: datetime,
    completed_close: Decimal | None,
    *,
    quote_max_age: timedelta = _DEFAULT_QUOTE_MAX_AGE,
) -> str | None:
    """Reason a chain cannot support an entry quote, before any rate lookup."""
    now = _as_utc(now)
    reason = displayed_chain_reason(chain, now)
    if reason is not None:
        return reason
    if regular_session_open(now):
        _price, _quote_time, spot_reason = _spot(chain, info, now, quote_max_age)
        return spot_reason
    if completed_close is None:
        return "Official completed-session close is unavailable"
    return None


def build_pricing_context(
    ticker: str,
    chain: OptionChainResponse,
    info: StockInfoResponse,
    now: datetime,
    completed_close: Decimal | None,
    curve: TreasuryCurve | None,
    dividends: DividendStatus,
    *,
    quote_max_age: timedelta = _DEFAULT_QUOTE_MAX_AGE,
) -> PricingContext | str:
    now = _as_utc(now)
    blocked = pricing_block_reason(
        chain, info, now, completed_close, quote_max_age=quote_max_age
    )
    if blocked is not None:
        return blocked
    session = quote_session(now)
    valuation_time = now if regular_session_open(now) else session_close(session)
    if regular_session_open(now):
        spot, underlying_quote_time, _spot_reason = _spot(chain, info, now, quote_max_age)
        stock_ask = info.ask if chain.source == "nasdaq" and spot is not None else None
    else:
        spot, underlying_quote_time, stock_ask = completed_close, None, None
    if spot is None or chain.source is None:
        return "Underlying quote time or price is unavailable"
    underlying_quote_fetched_at = None
    if underlying_quote_time is not None:
        underlying_quote_fetched_at = (
            chain.fetched_at if chain.source == "yahoo" else info.fetched_at
        )
    reasons: dict[str, str] = {}
    rates: dict[str, Decimal] = {}
    for expiry_text in {row.expiration for row in chain.rows}:
        expiry = date.fromisoformat(expiry_text)
        if expiry <= valuation_time.astimezone(_NY).date():
            reasons[expiry_text] = "Same-day option quote timing cannot be verified"
        elif (
            curve is None
            or (rate := curve.rate_for(expiry, valuation_time)) is None
            or not math.isfinite(rate)
            or not 0 <= rate <= 0.25
        ):
            reasons[expiry_text] = "Dated Treasury rate is unavailable"
        else:
            eligible, reason = dividends.eligible_for(expiry)
            if eligible:
                rates[expiry_text] = Decimal(str(rate))
            else:
                reasons[expiry_text] = reason or "Dividend exposure is unsupported"
    by_contract: dict[tuple[str, Decimal], list[OptionQuote]] = defaultdict(list)
    for row in chain.rows:
        by_contract[(row.expiration, row.strike)].append(row)
    standard_rows = {
        key: matches[0]
        for key, matches in by_contract.items()
        if len(matches) == 1 and matches[0].identity_reason is None and matches[0].root == ticker
    }
    entry_quotes: dict[tuple[str, str, Decimal], tuple[Decimal, Decimal]] = {}
    quote_issues: dict[tuple[str, str, Decimal], PricingIssue] = {}
    for (expiry, strike), row in standard_rows.items():
        for side in ("call", "put"):
            quote, issue = _entry_bid_ask(row, side, spot)
            if quote is not None:
                entry_quotes[(side, expiry, strike)] = quote
            elif issue is not None:
                quote_issues[(side, expiry, strike)] = issue
    identity_issues = {
        key: pricing_issue(
            "Contract appears more than once in the chain"
            if len(matches) > 1 else "Contract terms cannot be verified"
        )
        for key, matches in by_contract.items() if key not in standard_rows
    }
    return PricingContext(
        spot,
        stock_ask,
        session,
        chain.source,
        chain.fetched_at,
        valuation_time,
        underlying_quote_time,
        underlying_quote_fetched_at,
        curve.as_of if curve is not None else None,
        rates,
        reasons,
        entry_quotes,
        set(standard_rows),
        set(by_contract) - set(standard_rows),
        (
            "completed_session_close" if underlying_quote_time is None
            else "yahoo_regular_market_price" if chain.source == "yahoo"
            else "underlying_midpoint"
        ),
        quote_issues,
        identity_issues,
    )
