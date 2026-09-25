from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from options_api.chain import assemble_covered_calls, load_covered_calls
from options_api.greeks import compute_greeks
from options_api.memo import ContractMemo
from options_api.service import OptionChainService

from .synthetic import RATE, synthetic_context


def _contracts(page) -> int:
    return sum(len(expiration.contracts) for expiration in page.expirations)


def _spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"n": 0}
    real = compute_greeks

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr("options_api.chain.compute_greeks", wrapped)
    return calls


def test_second_identical_request_skips_greeks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    chain, info, history, today, now = synthetic_context()
    memo = ContractMemo()
    first = assemble_covered_calls(
        chain, info, history, today, now, moneyness="all", rate=RATE, memo=memo
    )
    built = calls["n"]
    assert built == _contracts(first)
    second = assemble_covered_calls(
        chain, info, history, today, now, moneyness="all", rate=RATE, memo=memo
    )
    assert calls["n"] == built
    assert second == first


def test_itm_then_all_builds_only_missing_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    chain, info, history, today, now = synthetic_context()
    memo = ContractMemo()
    itm = assemble_covered_calls(
        chain, info, history, today, now, moneyness="itm", rate=RATE, memo=memo
    )
    after_itm = calls["n"]
    assert after_itm == _contracts(itm)
    everything = assemble_covered_calls(
        chain, info, history, today, now, moneyness="all", rate=RATE, memo=memo
    )
    assert calls["n"] - after_itm == _contracts(everything) - _contracts(itm)
    assemble_covered_calls(
        chain, info, history, today, now, moneyness="itm", rate=RATE, memo=memo
    )
    assert calls["n"] == _contracts(everything)


def test_memo_invalidates_when_inputs_change(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    chain, info, history, today, now = synthetic_context()
    memo = ContractMemo()
    kwargs = {"moneyness": "all", "rate": RATE, "memo": memo}
    assemble_covered_calls(chain, info, history, today, now, **kwargs)
    baseline = calls["n"]

    refetched = chain.model_copy(update={"rows": list(chain.rows)})
    assemble_covered_calls(refetched, info, history, today, now, **kwargs)
    assert calls["n"] == baseline * 2

    richer = info.model_copy(update={"bid": Decimal("60")})
    assemble_covered_calls(chain, richer, history, today, now, **kwargs)
    assert calls["n"] == baseline * 3

    assemble_covered_calls(chain, info, history, today + timedelta(days=1), now, **kwargs)
    assert calls["n"] == baseline * 4

    assemble_covered_calls(
        chain, info, history, today, now, moneyness="all", rate=Decimal("0.05"), memo=memo
    )
    assert calls["n"] == baseline * 5


def test_memoized_page_matches_unmemoized_page() -> None:
    chain, info, history, today, now = synthetic_context()
    memoized = assemble_covered_calls(
        chain, info, history, today, now, moneyness="all", rate=RATE, memo=ContractMemo()
    )
    plain = assemble_covered_calls(
        chain, info, history, today, now, moneyness="all", rate=RATE
    )
    assert memoized == plain


def test_memo_evicts_the_least_recently_used_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    chain, info, history, today, now = synthetic_context()
    memo = ContractMemo(capacity=2)
    for ticker in ("AAA", "BBB", "CCC"):
        copied = chain.model_copy(update={"ticker": ticker})
        assemble_covered_calls(
            copied, info, history, today, now, moneyness="all", rate=RATE, memo=memo
        )
    filled = calls["n"]
    assemble_covered_calls(
        chain.model_copy(update={"ticker": "CCC"}),
        info,
        history,
        today,
        now,
        moneyness="all",
        rate=RATE,
        memo=memo,
    )
    assert calls["n"] == filled
    assemble_covered_calls(
        chain.model_copy(update={"ticker": "AAA"}),
        info,
        history,
        today,
        now,
        moneyness="all",
        rate=RATE,
        memo=memo,
    )
    assert calls["n"] > filled


def test_service_owns_a_memo() -> None:
    service = OptionChainService(client=object())  # type: ignore[arg-type]
    assert isinstance(service.memo, ContractMemo)


@pytest.mark.asyncio
async def test_loader_reuses_the_service_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    chain, info, history, _today, now = synthetic_context()

    class _Service:
        def __init__(self) -> None:
            self.memo = ContractMemo()

        async def get_chain(self, _ticker):
            return chain

        async def get_info(self, _ticker, _now=None):
            return info

        async def get_history(self, _ticker, _from_date):
            return history

    service = _Service()
    page = await load_covered_calls(service, "IREN", now, moneyness="all", rate=RATE)
    built = calls["n"]
    assert built == _contracts(page)
    again = await load_covered_calls(service, "IREN", now, moneyness="all", rate=RATE)
    assert calls["n"] == built
    assert again == page
