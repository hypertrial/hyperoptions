"""Reuse computed contracts while the cached chain rows and page inputs match."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from options_api.models import OptionQuote


@dataclass
class _Entry:
    rows: list[OptionQuote]
    current: Decimal
    lows: dict[str, Decimal | None]
    today: date
    rate: Decimal
    contracts: dict[tuple[str, Decimal], object]


class ContractMemo:
    """Contracts for one ticker and side, valid while rows identity and inputs match."""

    def __init__(self, capacity: int = 32) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    def contracts(
        self,
        ticker: str,
        side: str,
        rows: list[OptionQuote],
        current: Decimal,
        lows: dict[str, Decimal | None],
        today: date,
        rate: Decimal,
    ) -> dict[tuple[str, Decimal], object]:
        key = (ticker, side)
        entry = self._entries.get(key)
        if (
            entry is None
            or entry.rows is not rows
            or entry.current != current
            or entry.lows != lows
            or entry.today != today
            or entry.rate != rate
        ):
            entry = _Entry(rows, current, dict(lows), today, rate, {})
            self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)
        return entry.contracts
