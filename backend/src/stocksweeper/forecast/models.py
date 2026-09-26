"""Public forecast contract and frozen peer candidate identity."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Side = Literal["call", "put"]
State = Literal["long", "flat"]


class PeerCandidate(BaseModel):
    ticker: str
    sector: str | None = None


class ForecastSnapshot(BaseModel):
    ticker: str
    side: Side
    strike: Decimal
    expiry: date
    as_of: date
    status: Literal["available", "unavailable"] = "unavailable"
    itm_probability: float | None = Field(default=None, ge=0, le=1)
    reason: str | None = "model_not_ready"
    model_id: str | None = None
    strategy_id: str | None = None
    strategy_name: str | None = None
    signal_state: State | None = None
    cohort_size: int | None = None
    fit_peers: int | None = None
    audit_peers: int | None = None
    fit_samples: int | None = None
    audit_samples: int | None = None
    audit_blocks: int | None = None
    crps_skill_lower_90: float | None = None
    brier_delta: float | None = None
    source: str = (
        "Yahoo Finance daily Close (split-adjusted, dividend-unadjusted; auto_adjust=False)"
    )
    survivorship_note: str = "Peer cohort uses current Nasdaq listings; delisted stocks are absent."
    data_hash: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
