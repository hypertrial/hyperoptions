"""Conservative, prospective calibration for displayed physical probabilities."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from math import isfinite
from statistics import mean
from typing import Any

from options_api.contract_identity import parse_watch_key
from options_api.market_calendar import session_close
from options_api.outcomes import TERMS_NOTE, classify
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.ledger import ForecastLedger

CalibrationKey = tuple[str, str, str, str, str]
_BANDS = {"1": (1, 1), "2-5": (2, 5), "6-25": (6, 25), "26-252": (26, 252)}
_LOOKBACK = timedelta(days=4 * 366)


@lru_cache(maxsize=2048)
def _close(session: date) -> datetime:
    return session_close(session)


def horizon_band(horizon: int) -> str | None:
    return next((name for name, (low, high) in _BANDS.items() if low <= horizon <= high), None)


def moneyness_band(spot: Decimal, strike: Decimal, side: str) -> str | None:
    if spot <= 0 or strike <= 0:
        return None
    relative = abs(strike / spot - 1)
    if relative <= Decimal("0.05"):
        return "near ATM"
    in_money = (side == "call" and spot > strike) or (side == "put" and spot < strike)
    direction = "ITM" if in_money else "OTM"
    return f"moderately {direction}" if relative <= Decimal("0.15") else f"far {direction}"


def _valid(row: dict[str, Any], as_of: datetime) -> bool:
    """Recheck exact source evidence rather than trusting weak SQL constraints."""
    try:
        issued = row["issued_at"]
        checked = row["label_checked_at"]
        retrieved = row["input_retrieved_at"]
        origin = row["input_session"]
        expiry = row["expiry_session"]
        strike = Decimal(row["strike_exact"])
        spot = Decimal(row["spot_exact"])
        nasdaq = Decimal(row["nasdaq_close_exact"])
        yahoo = Decimal(row["yahoo_close_exact"])
        selected = Decimal(row["selected_close_exact"])
        probability = float(row["itm_probability"])
        parsed = parse_watch_key(row["contract_key"])
        return bool(
            isinstance(issued, datetime)
            and isinstance(checked, datetime)
            and isinstance(origin, date)
            and isinstance(expiry, date)
            and issued.tzinfo is not None
            and checked.tzinfo is not None
            and isinstance(retrieved, datetime)
            and retrieved.tzinfo is not None
            and _close(origin) <= retrieved <= issued
            and issued <= as_of
            and issued < checked <= as_of
            and issued < _close(expiry)
            and origin < expiry
            and row["terms_note"] == TERMS_NOTE
            and row["root"] == row["ticker"]
            and row["label_status"] == "valid"
            and row["label_source"] == "Nasdaq historical Close (Yahoo cross-check)"
            and parsed
            == (
                row["ticker"], row["root"], row["side"],
                row["expiration"].isoformat(), strike,
            )
            and strike > 0
            and spot > 0
            and nasdaq > 0
            and selected == nasdaq
            and abs(nasdaq - yahoo) <= Decimal("0.005")
            and row["classification"] == classify(row["side"], selected, strike)
            and row["model_version"]
            and row["data_hash"]
            and row["price_basis"] == "completed_close"
            and row["snapshot_window"] is None
            and isfinite(probability)
            and 0 <= probability <= 1
        )
    except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation):
        return False


def _tenths(probability: float) -> int:
    return int((Decimal(str(probability)) * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def build_calibration(
    ledger: ForecastLedger, as_of: datetime
) -> dict[CalibrationKey, dict[str, object]]:
    """Build once daily from matured as-issued labels; each close is one unit.

    Calendar dates are embargoed by the band's maximum trading horizon. A
    ticker-origin-horizon contributes one equally weighted mean across its
    strikes and call/put sides, even if many contracts share its close.
    """
    if as_of.tzinfo is None:
        raise ValueError("calibration as_of must be timezone-aware")
    calendar = SessionCalendar()
    @lru_cache(maxsize=65_536)
    def horizon(origin: date, expiration: date) -> int:
        return calendar.horizon(origin, expiration)

    earliest: dict[tuple[str, str, date, str], dict[str, Any]] = {}
    for row in ledger.calibration_rows(since=as_of.date() - _LOOKBACK, label_as_of=as_of):
        if not _valid(row, as_of):
            continue
        # Fixed before the outcome: the first same-session issuance wins if
        # a source revision creates another version of the same contract.
        key = (
            row["contract_key"], row["model_version"], row["input_session"],
            row["price_basis"],
        )
        previous = earliest.get(key)
        if previous is None or (row["issued_at"], row["idempotency_key"]) < (
            previous["issued_at"], previous["idempotency_key"]
        ):
            earliest[key] = row

    # Keep one input vintage per ticker-origin-horizon-model cohort.
    vintage: dict[tuple[str, date, int, str, str], tuple[datetime, str, str]] = {}
    candidates: list[tuple[dict[str, Any], int, str, str]] = []
    for row in earliest.values():
        sessions = horizon(row["input_session"], row["expiration"])
        band = horizon_band(sessions)
        moneyness = moneyness_band(
            Decimal(row["spot_exact"]), Decimal(row["strike_exact"]), row["side"]
        )
        if band is None or moneyness is None:
            continue
        unit = (
            row["ticker"], row["input_session"], sessions,
            row["model_version"], row["price_basis"],
        )
        choice = (row["issued_at"], row["idempotency_key"], row["data_hash"])
        if unit not in vintage or choice < vintage[unit]:
            vintage[unit] = choice
        candidates.append((row, sessions, band, moneyness))

    grouped: dict[
        CalibrationKey, dict[date, dict[tuple[str, date, int], list[tuple[float, float, date]]]]
    ] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for row, sessions, band, moneyness in candidates:
        unit = (
            row["ticker"], row["input_session"], sessions,
            row["model_version"], row["price_basis"],
        )
        if row["data_hash"] != vintage[unit][2]:
            continue
        # Calls and puts at the same strike have complementary labels. Mixing
        # them can make a poorly calibrated model appear perfectly calibrated.
        key = (row["model_version"], row["price_basis"], band, moneyness, row["side"])
        grouped[key][row["input_session"]][unit[:3]].append(
            (
                float(row["itm_probability"]),
                float(row["classification"] == "itm"),
                row["expiry_session"],
            )
        )

    evidence: dict[CalibrationKey, dict[str, object]] = {}
    for key, dates in grouped.items():
        selected: list[date] = []
        next_allowed: date | None = None
        for origin in sorted(dates):
            if next_allowed is None or origin >= next_allowed:
                selected.append(origin)
                next_allowed = calendar.offset(origin, _BANDS[key[2]][1] + 1)
        units = [
            (ticker, mean(p for p, _, _ in contracts), mean(y for _, y, _ in contracts),
             max(expiry for _, _, expiry in contracts))
            for origin in selected
            for (ticker, _, _), contracts in dates[origin].items()
        ]
        if len({unit[0] for unit in units}) < 20 or len(selected) < 20 or len(units) < 500:
            continue
        evidence[key] = {
            "source": "prospective_as_issued",
            "model_version": key[0],
            "horizon_band": key[2],
            "moneyness_band": key[3],
            "option_side": key[4],
            "independent_units": len(units),
            "predicted_itm_pct_tenths": _tenths(mean(unit[1] for unit in units)),
            "observed_itm_pct_tenths": _tenths(mean(unit[2] for unit in units)),
            "through_session": max(unit[3] for unit in units),
        }
    return evidence
