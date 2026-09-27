"""Record prospective quote-conditioned forecasts with as-issued provenance."""

from __future__ import annotations

import hashlib
import json
import math
from time import perf_counter
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from options_api.intraday_shadow import forecast_intraday_shadow
from options_api.market_calendar import _calendar
from options_api.market_watch import MarketWatchOdds, UnderlyingQuote
from options_api.outcomes import TERMS_NOTE
from stocksweeper.forecast.ledger import ForecastIssuance, ForecastLedger
from stocksweeper.forecast.predictive import PredictiveDistribution, PredictiveForecaster

Window = Literal["10:00", "13:00", "15:30"]
_NY = ZoneInfo("America/New_York")
_WINDOW_TIMES = {"10:00": time(10), "13:00": time(13), "15:30": time(15, 30)}
_TOLERANCE = timedelta(minutes=5)
_INTRADAY_VERSION = "intraday-open-close-ewma60-shadow-v1"
_COMPARATOR_VERSION = "quote-reanchored-comparator-v1"


def _quote_digest(quote: UnderlyingQuote) -> str:
    evidence = {
        "quote_source": quote.source,
        "quote_time": quote.quote_time.isoformat(),
        "quote_fetched_at": quote.fetched_at.isoformat(),
        "quote_spot": str(quote.spot),
    }
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _quote_reason(
    quote: UnderlyingQuote | None,
    *,
    now: datetime,
    target: datetime,
    opened: datetime,
    closed: datetime,
) -> str | None:
    if target < opened or target >= closed or now >= closed:
        return "snapshot_window_outside_regular_session"
    if quote is None:
        return "underlying_quote_unavailable"
    if quote.quote_time.tzinfo is None or quote.fetched_at.tzinfo is None:
        return "underlying_quote_time_unverified"
    if quote.session_date != target.astimezone(_NY).date():
        return "underlying_quote_session_mismatch"
    if not opened <= quote.quote_time < closed or not quote.source:
        return "underlying_quote_outside_session"
    if not quote.spot.is_finite() or quote.spot <= 0:
        return "underlying_quote_invalid"
    if quote.quote_time > now or quote.fetched_at > now:
        return "underlying_quote_in_future"
    if quote.fetched_at < quote.quote_time:
        return "underlying_quote_fetch_precedes_timestamp"
    if abs(quote.quote_time - target) > _TOLERANCE:
        return "underlying_quote_outside_snapshot_window"
    if now - quote.quote_time > _TOLERANCE:
        return "underlying_quote_stale"
    return None


def _comparator(
    base: PredictiveDistribution, quote: UnderlyingQuote
) -> PredictiveDistribution | None:
    if base.spot is None or base.spot <= 0 or base.data_hash is None:
        return None
    anchor = float(quote.spot)
    prices = tuple(anchor * price / base.spot for price in base.prices)
    if not prices or any(not math.isfinite(price) or price <= 0 for price in prices):
        return None
    return replace(
        base,
        spot=anchor,
        terminal_prices=prices,
        model_version=_COMPARATOR_VERSION,
        data_hash=hashlib.sha256(
            f"{base.data_hash}:{_quote_digest(quote)}:quote_reanchored_comparator".encode()
        ).hexdigest(),
    )


def _probabilities(
    distribution: PredictiveDistribution, issue: ForecastIssuance
) -> tuple[float, float, float] | None:
    strike = Decimal(issue.strike_exact)
    call = distribution.probability("call", strike)
    put = distribution.probability("put", strike)
    if (
        call is None
        or put is None
        or not math.isfinite(call)
        or not math.isfinite(put)
        or not 0 <= call <= 1
        or not 0 <= put <= 1
        or call + put > 1 + 1e-8
    ):
        return None
    return (
        (call, put, max(0.0, 1 - call - put))
        if issue.side == "call"
        else (put, call, max(0.0, 1 - call - put))
    )


def capture_intraday_window(
    ledger: ForecastLedger,
    forecaster: PredictiveForecaster,
    market_odds: MarketWatchOdds,
    watched_issues: Iterable[tuple[ForecastIssuance, PredictiveDistribution | None]],
    *,
    now: datetime,
    window: Window,
) -> int:
    """Capture one real-time window for watched contracts; return inserted rows.

    The caller supplies the same completed-close issuances used on the watchlist.
    Its scheduler must call at the actual window, never replay a past clock as
    as-issued evidence, and exclude watches created after the target time.
    """
    if now.tzinfo is None:
        raise ValueError("capture time must be timezone-aware")
    if window not in _WINDOW_TIMES:
        raise ValueError("unsupported intraday snapshot window")
    now = now.astimezone(UTC)
    day = now.astimezone(_NY).date()
    target = datetime.combine(day, _WINDOW_TIMES[window], _NY).astimezone(UTC)
    if not target <= now < target + _TOLERANCE:
        return 0
    calendar = _calendar()
    if not calendar.is_session(day.isoformat()):
        return 0
    session = calendar.date_to_session(day.isoformat(), direction="none")
    opened = calendar.session_open(session).to_pydatetime()
    closed = calendar.session_close(session).to_pydatetime()
    prior = forecaster.calendar.offset(day, -1)
    quote_cache: dict[str, UnderlyingQuote | None] = {}
    retrieval_cache: dict[tuple[str, str], datetime | None] = {}
    model_cache: dict[
        tuple[int, str], tuple[PredictiveDistribution | None, str | None, float]
    ] = {}
    entries: list[tuple[ForecastIssuance, PredictiveDistribution | None]] = []

    for primary, base in watched_issues:
        if primary.ticker not in quote_cache:
            try:
                quote_cache[primary.ticker] = market_odds.underlying_quote(primary.ticker)
            except Exception:
                quote_cache[primary.ticker] = None
        quote = quote_cache[primary.ticker]
        reason = _quote_reason(quote, now=now, target=target, opened=opened, closed=closed)
        if primary.expiry_session < day:
            continue
        if reason == "snapshot_window_outside_regular_session":
            pass
        elif primary.root != primary.ticker or primary.terms_note != TERMS_NOTE:
            reason = "contract_terms_ambiguous"
        elif primary.input_session != prior or base is None or primary.status != "available":
            reason = "completed_close_forecast_unavailable"
        elif (
            base.status != "available"
            or base.as_of != prior
            or base.expiry_session != primary.expiry_session
            or base.data_hash != primary.data_hash
        ):
            reason = "completed_close_input_mismatch"
        same_origin = primary.input_session == prior and base is not None and base.as_of == prior
        retrieved_at = primary.input_retrieved_at if same_origin else None
        if reason is None:
            assert base is not None and base.data_hash is not None
            key = (primary.ticker, base.data_hash)
            if key not in retrieval_cache:
                try:
                    retrieval_cache[key] = ledger.cache_retrieved_at(
                        primary.ticker, base.data_hash, prior
                    )
                except (OSError, ValueError):
                    retrieval_cache[key] = None
            retrieved_at = retrieval_cache[key]
            if retrieved_at is None or retrieved_at > now:
                reason = "completed_close_input_provenance_unverified"

        for method, version in (
            ("quote_reanchored_comparator", _COMPARATOR_VERSION),
            ("intraday_shadow", _INTRADAY_VERSION),
        ):
            candidate = None
            candidate_reason = reason
            lookup_ms = None
            if candidate_reason is None:
                assert base is not None and quote is not None
                key = (id(base), method)
                if key not in model_cache:
                    started = perf_counter()
                    try:
                        if method == "quote_reanchored_comparator":
                            result = _comparator(base, quote)
                            model_cache[key] = (
                                result,
                                None if result is not None else "comparator_scenarios_invalid",
                                (perf_counter() - started) * 1000,
                            )
                        else:
                            result = forecast_intraday_shadow(forecaster, base, quote)
                            model_cache[key] = (
                                result.distribution,
                                result.reason,
                                (perf_counter() - started) * 1000,
                            )
                    except (OSError, ValueError, OverflowError):
                        model_cache[key] = (
                            None,
                            "intraday_model_failed",
                            (perf_counter() - started) * 1000,
                        )
                candidate, candidate_reason, lookup_ms = model_cache[key]
            probabilities = _probabilities(candidate, primary) if candidate is not None else None
            if candidate is not None and probabilities is None:
                candidate = None
                candidate_reason = "model_probability_invalid"
            metadata_quote = (
                quote
                if quote is not None
                and quote.quote_time.tzinfo is not None
                and quote.fetched_at.tzinfo is not None
                and quote.quote_time <= quote.fetched_at <= now
                and quote.source
                else None
            )
            issuance = replace(
                primary,
                input_session=prior,
                input_retrieved_at=retrieved_at,
                issued_at=max(now, primary.issued_at),
                model_version=version,
                method=method,
                data_hash=primary.data_hash if same_origin else None,
                distribution_hash=None,
                price_basis="underlying_quote",
                spot_exact=str(quote.spot) if quote is not None else None,
                status="available" if candidate is not None else "unavailable",
                itm_probability=probabilities[0] if probabilities else None,
                otm_probability=probabilities[1] if probabilities else None,
                atm_probability=probabilities[2] if probabilities else None,
                unavailable_reason=None
                if candidate is not None
                else (candidate_reason or "intraday_model_unavailable"),
                quote_time=metadata_quote.quote_time if metadata_quote else None,
                quote_source=metadata_quote.source if metadata_quote else None,
                quote_fetched_at=metadata_quote.fetched_at if metadata_quote else None,
                quote_digest=_quote_digest(metadata_quote) if metadata_quote else None,
                snapshot_window=window,
                prepare_ms=None,
                lookup_ms=lookup_ms,
                idempotency_key=None,
            )
            entries.append((issuance, candidate))
    return ledger.record_batch(entries)
