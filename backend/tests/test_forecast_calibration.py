from __future__ import annotations

from datetime import date
from math import cos, sin

import numpy as np
import pytest

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.calibration import (
    Observation,
    _empirical_crps,
    fit_audit,
    tail_probability,
)


def _observations(
    prefix: str,
    peers: int,
    start: date,
    end: date,
    days: int,
    *,
    horizon: int = 1,
    alternate_states: bool = False,
) -> list[Observation]:
    calendar = SessionCalendar()
    sessions = calendar.sessions(start, end)
    result = []
    for peer in range(peers):
        for index, day in enumerate(sessions[:days]):
            state = "flat" if alternate_states and index % 2 else "long"
            center = -0.3 if state == "flat" else 0.3
            noise = 0.015 * sin(index / 3 + peer / 7) + 0.005 * cos(peer)
            result.append(
                Observation(
                    ticker=f"{prefix}{peer:03d}",
                    as_of=day,
                    maturity=calendar.offset(day, horizon),
                    horizon=horizon,
                    state=state,
                    standardized_return=center + noise,
                )
            )
    return result


def test_strict_tails_are_monotone_and_exclude_equality() -> None:
    distribution = (-1.0, 0.0, 0.0, 2.0)
    assert tail_probability(distribution, 0.0, "call") == 0.25
    assert tail_probability(distribution, 0.0, "put") == 0.25
    call = [tail_probability(distribution, strike, "call") for strike in (-2, -1, 0, 1, 2)]
    put = [tail_probability(distribution, strike, "put") for strike in (-2, -1, 0, 1, 2)]
    assert call == sorted(call, reverse=True)
    assert put == sorted(put)


def test_crps_matches_single_member_and_pairwise_definition() -> None:
    assert _empirical_crps((1.0,), np.array([3.0])).tolist() == [2.0]
    assert _empirical_crps((0.0, 1.0), np.array([0.0])).tolist() == [0.25]


def test_passing_conditional_distribution_has_audited_skill() -> None:
    fit = _observations(
        "FIT",
        60,
        date(2021, 1, 4),
        date(2022, 12, 20),
        220,
        alternate_states=True,
    )
    audit = _observations("AUD", 20, date(2023, 1, 3), date(2025, 12, 20), 200)
    result = fit_audit(fit, audit, 1, "long")
    assert result.fit_peers == 60
    assert result.audit_peers == 20
    assert result.audit_blocks >= 30
    assert result.fit_samples == 60 * 110
    assert result.audit_samples == len(audit)
    assert result.qualified, result.reason
    assert result.crps_skill_lower_90 is not None and result.crps_skill_lower_90 > 0
    assert result.brier_delta is not None and result.brier_delta <= 0
    assert result.support_low is not None and result.support_high is not None
    assert result.support_low < result.support_high


def test_sparse_peer_or_calendar_block_state_is_unavailable() -> None:
    fit = _observations("FIT", 49, date(2021, 1, 4), date(2022, 12, 20), 220)
    audit = _observations("AUD", 20, date(2023, 1, 3), date(2025, 12, 20), 200)
    assert fit_audit(fit, audit, 1, "long").reason == "insufficient_fit_peers"
    fit.extend(_observations("MORE", 1, date(2021, 1, 4), date(2022, 12, 20), 220))
    short_audit = _observations("AUD", 20, date(2023, 1, 3), date(2025, 12, 20), 10)
    assert fit_audit(fit, short_audit, 1, "long").reason == "insufficient_audit_blocks"


def test_uninformative_signal_cannot_publish_a_probability() -> None:
    fit = _observations("FIT", 60, date(2021, 1, 4), date(2022, 12, 20), 220)
    audit = _observations("AUD", 20, date(2023, 1, 3), date(2025, 12, 20), 200)
    result = fit_audit(fit, audit, 1, "long")
    assert not result.qualified
    assert result.reason == "crps_audit_failed"
    assert result.crps_skill_lower_90 == 0


def test_long_horizon_audit_does_not_count_overlapping_returns_as_independent_blocks() -> None:
    fit = _observations("FIT", 60, date(2021, 1, 4), date(2022, 12, 20), 220, horizon=25)
    audit = _observations("AUD", 20, date(2023, 1, 3), date(2025, 12, 20), 700, horizon=25)
    result = fit_audit(fit, audit, 25, "long")
    assert result.fit_peers == 60
    assert result.audit_peers == 20
    assert result.audit_blocks < 30
    assert result.audit_samples < len(audit)
    assert result.reason == "insufficient_audit_blocks"


def test_25_session_horizon_can_reach_30_nonoverlapping_audit_blocks() -> None:
    calendar = SessionCalendar()
    sessions = calendar.sessions(date(2023, 1, 1), date(2025, 12, 31))
    assert len(sessions) >= 751
    fit = _observations("FIT", 60, date(2021, 1, 4), date(2022, 12, 20), 220, horizon=25)
    audit = [
        Observation(
            f"AUD{peer:03d}",
            sessions[block * 25],
            sessions[(block + 1) * 25],
            25,
            "long",
            0.3 + 0.001 * peer,
        )
        for peer in range(20)
        for block in range(30)
    ]
    result = fit_audit(fit, audit, 25, "long")
    assert result.audit_peers == 20
    assert result.audit_blocks == 30
    assert result.audit_samples == 600
    assert result.reason != "insufficient_audit_blocks"


def test_fit_audit_rejects_ticker_or_time_leakage_and_duplicate_grain() -> None:
    calendar = SessionCalendar()
    fit_day = date(2021, 1, 4)
    audit_day = date(2023, 1, 3)
    fit = [Observation("AAA", fit_day, calendar.offset(fit_day, 1), 1, "long", 0.1)]
    audit = [Observation("AAA", audit_day, calendar.offset(audit_day, 1), 1, "long", 0.1)]
    with pytest.raises(ValueError, match="disjoint"):
        fit_audit(fit, audit, 1, "long")
    with pytest.raises(ValueError, match="duplicate"):
        fit_audit(fit * 2, (), 1, "long")
    wrong_maturity = [Observation("AAA", fit_day, calendar.offset(fit_day, 2), 1, "long", 0.1)]
    with pytest.raises(ValueError, match="scheduled-session"):
        fit_audit(wrong_maturity, (), 1, "long")
    future_label = [Observation("AAA", audit_day, calendar.offset(audit_day, 1), 1, "long", 0.1)]
    with pytest.raises(ValueError, match="outside"):
        fit_audit(future_label, (), 1, "long")
