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
    # Keep chain financial figures here; odds/risk and watch identity have
    # separate tests and are unavailable in this direct assembler fixture.
    for page in pages.values():
        assert page.pop("chain_source") == "nasdaq"
        assert page.pop("chain_fetched_at") == page["fetched_at"]
        for group in page["expirations"]:
            for contract in group["contracts"]:
                assert contract.pop("market_odds") == {
                    "status": "pending", "itm_pct_tenths": None, "otm_pct_tenths": None,
                    "reason": None, "source": None, "fetched_at": None,
                    "session_date": None, "model_version": None,
                }
                assert contract.pop("predictive_odds")["status"] == "pending"
                assert contract.pop("hypothetical_risk")["status"] == "unavailable"
                assert contract.pop("greeks_rate_pct_tenths") is None
                assert contract.pop("greeks_rate_as_of_session") is None
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
