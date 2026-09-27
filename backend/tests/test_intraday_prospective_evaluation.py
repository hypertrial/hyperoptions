from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from math import log
from zoneinfo import ZoneInfo

import pytest

from scripts.evaluate_intraday_prospective import evaluate
from options_api.market_calendar import session_on_or_before

_NY = ZoneInfo("America/New_York")
_DAY = date(2026, 9, 28)
_EXPIRY = date(2026, 9, 30)


def _rows(
    contract: str = "TEST-C-100",
    *,
    day: date = _DAY,
    expiry: date = _EXPIRY,
    window: str = "10:00",
    side: str = "call",
    observed: bool = True,
    probabilities: tuple[float, float, float] = (0.6, 0.7, 0.8),
) -> list[dict[str, object]]:
    hour, minute = map(int, window.split(":"))
    target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=_NY).astimezone(UTC)
    issue = target + timedelta(minutes=1)
    label = datetime(expiry.year, expiry.month, expiry.day, 22, tzinfo=UTC)
    prior = session_on_or_before(day - timedelta(days=1))
    base = {
        "contract_key": contract,
        "ticker": "TEST",
        "root": "TEST",
        "side": side,
        "expiration": expiry,
        "expiry_session": expiry,
        "strike_exact": "100.000",
        "terms_note": "standard 100-share terms",
        "contract_since": prior,
        "input_session": prior,
        "input_retrieved_at": target - timedelta(minutes=10),
        "data_hash": "a" * 64,
        "status": "available",
        "unavailable_reason": None,
        "provenance": "as_issued",
        "label_status": "valid",
        "label_reason": None,
        "label_checked_at": label,
        "observed_itm": observed,
        "quote_time": target + timedelta(seconds=30),
        "quote_source": "nasdaq",
        "quote_fetched_at": target + timedelta(seconds=31),
        "quote_digest": "b" * 64,
        "lookup_ms": 12.0,
    }
    return [
        {
            **base,
            "idempotency_key": f"{contract}-base",
            "method": "lognormal_ewma",
            "price_basis": "completed_close",
            "snapshot_window": None,
            "issued_at": issue,
            "itm_probability": probabilities[0],
            "quote_time": None,
            "quote_source": None,
            "quote_fetched_at": None,
            "quote_digest": None,
        },
        {
            **base,
            "idempotency_key": f"{contract}-comparator",
            "method": "quote_reanchored_comparator",
            "price_basis": "underlying_quote",
            "snapshot_window": window,
            "issued_at": issue + timedelta(seconds=1),
            "itm_probability": probabilities[1],
        },
        {
            **base,
            "idempotency_key": f"{contract}-shadow",
            "method": "intraday_shadow",
            "price_basis": "underlying_quote",
            "snapshot_window": window,
            "issued_at": issue + timedelta(seconds=1),
            "itm_probability": probabilities[2],
            "lookup_ms": 18.0,
        },
    ]


def _as_of(expiry: date) -> datetime:
    return datetime(expiry.year, expiry.month, expiry.day, 23, tzinfo=UTC)


def test_scores_exact_triplets_once_and_averages_correlated_contracts() -> None:
    rows = _rows()
    rows += _rows(
        "TEST-P-100",
        side="put",
        observed=False,
        probabilities=(0.4, 0.3, 0.2),
    )
    retry = rows[2].copy()
    retry["idempotency_key"] = "late-bad-retry"
    retry["issued_at"] += timedelta(seconds=20)
    retry["itm_probability"] = 0.01
    rows.append(retry)
    report = evaluate(rows, as_of=_as_of(_EXPIRY))
    overall = report["overall"]
    assert report["duplicate_challenger_attempts_excluded"] == 1
    assert overall["recorded_contract_windows"] == 2
    assert overall["scored_contract_windows"] == 2
    assert overall["scored_ticker_date_window_expiry_units"] == 1
    assert overall["mean"]["brier"] == pytest.approx(
        {"dated_close": 0.16, "quote_reanchored_comparator": 0.09, "intraday_shadow": 0.04}
    )
    assert overall["mean"]["log_loss"]["intraday_shadow"] == pytest.approx(-log(0.8))
    assert overall["paired_shadow_minus_reference"]["brier"]["dated_close"] == pytest.approx(-0.12)
    assert overall["paired_calendar_date_bootstrap_95"] is None
    assert overall["latency_ms"]["intraday_shadow"] == {"p50": 18.0, "p95": 18.0}
    assert report["by_window"]["10:00"]["scored_contract_windows"] == 2
    assert report["by_window"]["13:00"]["recorded_contract_windows"] == 0


def test_vintage_quote_and_label_mismatch_are_rejected_without_cherry_picking() -> None:
    vintage = _rows("VINTAGE")
    vintage[2]["data_hash"] = "c" * 64
    quote = _rows("QUOTE")
    quote[2]["quote_digest"] = "d" * 64
    revised = _rows("REVISED")
    revised[0]["data_hash"] = "e" * 64
    excluded = _rows("EXCLUDED")
    for row in excluded:
        row.update(label_status="excluded", label_reason="split_affected_label", observed_itm=None)
    unmatured = _rows("UNMATURED")
    for row in unmatured:
        row["label_checked_at"] = row["issued_at"]
    report = evaluate(vintage + quote + revised + excluded + unmatured, as_of=_as_of(_EXPIRY))
    reasons = report["overall"]["rejection_reasons"]
    assert reasons == {
        "challenger_input_vintage_mismatch": 1,
        "quote_vintage_missing_or_mismatch": 1,
        "baseline_input_vintage_mismatch": 1,
        "split_affected_label": 1,
        "label_not_mature_at_scoring": 1,
    }
    assert report["overall"]["scored_contract_windows"] == 0
    assert report["overall"]["triple_available_contract_windows"] == 4


def test_idempotent_close_forecast_issued_before_window_still_pairs() -> None:
    rows = _rows("EARLIER-CLOSE")
    rows[0]["issued_at"] -= timedelta(hours=8)
    for row in rows:
        row["input_retrieved_at"] -= timedelta(days=1)
    rows[0]["input_retrieved_at"] -= timedelta(days=1)  # Identical bars were refetched.
    report = evaluate(rows, as_of=_as_of(_EXPIRY))
    assert report["overall"]["scored_contract_windows"] == 1
    assert report["overall"]["rejection_reasons"] == {}


def test_same_day_early_close_and_holiday_strata_do_not_invent_scores() -> None:
    same = _rows("SAME", expiry=_DAY)
    same_report = evaluate(same, as_of=_as_of(_DAY))
    assert same_report["by_expiry"]["same_day"]["scored_contract_windows"] == 1

    early = date(2026, 11, 27)  # Post-Thanksgiving early close.
    early_rows = _rows("EARLY", day=early, expiry=early, window="15:30")
    for row in early_rows[1:]:
        row.update(
            status="unavailable", unavailable_reason="snapshot_window_outside_regular_session"
        )
    early_report = evaluate(early_rows, as_of=_as_of(early))
    assert early_report["by_calendar"]["early_close"]["recorded_contract_windows"] == 1
    assert early_report["overall"]["rejection_reasons"] == {
        "snapshot_window_outside_regular_session": 1
    }

    holiday = date(2026, 11, 26)
    holiday_rows = _rows("HOLIDAY", day=holiday, expiry=early)
    holiday_report = evaluate(holiday_rows, as_of=_as_of(early))
    assert holiday_report["by_calendar"]["holiday_or_non_session"]["recorded_contract_windows"] == 1
    assert holiday_report["overall"]["rejection_reasons"] == {"snapshot_outside_regular_session": 1}


def test_unrecorded_windows_and_unmatured_labels_are_explicit() -> None:
    empty = evaluate([], as_of=_as_of(_EXPIRY))
    assert empty["overall"]["recorded_contract_windows"] == 0
    assert "Missed windows" in empty["denominator_limit"]
    future = evaluate(_rows(), as_of=datetime(2026, 9, 29, tzinfo=UTC))
    assert future["overall"]["scored_contract_windows"] == 0
    assert future["overall"]["rejection_reasons"] == {"label_not_mature_at_scoring": 1}
