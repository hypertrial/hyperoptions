from __future__ import annotations

import json
from pathlib import Path

from options_api.chain import assemble_cash_secured_puts, assemble_covered_calls

from .synthetic import RATE, synthetic_context

FIXTURES = Path(__file__).parent / "fixtures"


def _pages(assemble) -> dict[str, object]:
    chain, info, history, today, now = synthetic_context()
    pages = {
        moneyness: assemble(
            chain,
            info,
            history,
            today,
            now,
            moneyness=moneyness,
            name="Iris Energy Limited",
            rate=RATE,
        ).model_dump(mode="json")
        for moneyness in ("all", "itm", "otm")
    }
    # Keep the original financial golden fixtures intact; watch fields have
    # their own contract identity and moneyness tests.
    for page in pages.values():
        for group in page["expirations"]:
            for contract in group["contracts"]:
                for field in (
                    "at_the_money", "strike_exact", "watch_key", "watchability_reason"
                ):
                    contract.pop(field)
    return pages


def test_covered_call_pages_match_the_golden_fixture() -> None:
    expected = json.loads((FIXTURES / "golden_calls.json").read_text())
    assert _pages(assemble_covered_calls) == expected


def test_cash_secured_put_pages_match_the_golden_fixture() -> None:
    expected = json.loads((FIXTURES / "golden_puts.json").read_text())
    assert _pages(assemble_cash_secured_puts) == expected
