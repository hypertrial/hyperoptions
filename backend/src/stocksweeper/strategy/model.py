"""Typed strategy documents. The id is a content hash so identical rules dedupe."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

Op = Literal["gt", "lt", "gte", "lte", "crosses_above", "crosses_below"]
Logic = Literal["AND", "OR"]


class Rhs(BaseModel):
    kind: Literal["const", "indicator"]
    value: float | None = None
    indicator: str | None = None
    params: dict[str, int | float] = Field(default_factory=dict)


class Condition(BaseModel):
    indicator: str
    params: dict[str, int | float] = Field(default_factory=dict)
    op: Op
    rhs: Rhs


class ConditionGroup(BaseModel):
    logic: Logic
    conditions: list[Condition]


class ExitRule(BaseModel):
    """One exit for the whole rule. A stop replaces the signal's mirror exit."""

    kind: Literal["mirror", "atr_trail", "time"] = "mirror"
    atr_length: int = 14
    atr_mult: float = 3.0
    bars: int = 10


class Strategy(BaseModel):
    id: str
    name: str
    family: str
    entry: ConditionGroup
    exit: ConditionGroup
    parameters: dict[str, int | float]
    signals: str
    exit_rule: ExitRule = Field(default_factory=ExitRule)
    signal: str | None = None
    filters: list[str] = Field(default_factory=list)


def content_id(
    *,
    family: str,
    entry: ConditionGroup,
    exit_group: ConditionGroup,
    parameters: dict[str, int | float],
    exit_rule: ExitRule | None = None,
) -> str:
    payload: dict[str, object] = {
        "entry": entry.model_dump(mode="json"),
        "exit": exit_group.model_dump(mode="json"),
        "family": family,
        "parameters": parameters,
    }
    # A mirror rule hashes the same bytes as a definition stored before exit rules.
    if exit_rule is not None and exit_rule.kind != "mirror":
        payload["exit_rule"] = exit_rule.model_dump(mode="json")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def const(value: float) -> Rhs:
    return Rhs(kind="const", value=float(value))


def ref(indicator: str, params: Mapping[str, int | float] | None = None) -> Rhs:
    return Rhs(kind="indicator", indicator=indicator, params=dict(params or {}))


def cond(
    indicator: str,
    op: Op,
    rhs: Rhs,
    params: Mapping[str, int | float] | None = None,
) -> Condition:
    return Condition(indicator=indicator, params=dict(params or {}), op=op, rhs=rhs)
