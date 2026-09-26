from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from options_api.hypothetical_risk import compute_hypothetical_risk


def distribution(prices=(80.0, 100.0, 120.0), weights=(0.2, 0.5, 0.3)):
    return SimpleNamespace(status="available", spot=100.0, prices=prices, weights=weights)


def test_covered_call_expiry_payoff_and_lower_tail() -> None:
    result = compute_hypothetical_risk(
        distribution(),
        side="call",
        strike=Decimal("110"),
        spot=Decimal("100"),
        bid=Decimal("5"),
        quote_source="nasdaq",
        quote_session=date(2026, 9, 25),
    )

    assert result.status == "available"
    assert result.expected_pnl_cents == 40_000
    assert result.expected_return_pct_tenths == 42
    assert result.loss_pct_tenths == 200
    assert result.p05_pnl_cents == -150_000
    assert result.assumed_spot_cents == 10_000
    assert result.assumed_bid_cents == 500


def test_cash_secured_put_expiry_payoff_and_zero_pnl() -> None:
    result = compute_hypothetical_risk(
        distribution(),
        side="put",
        strike=Decimal("100"),
        spot=Decimal("100"),
        bid=Decimal("4"),
        quote_source="nasdaq",
        quote_session=date(2026, 9, 25),
    )

    assert result.status == "available"
    assert result.expected_pnl_cents == 0
    assert result.expected_return_pct_tenths == 0
    assert result.loss_pct_tenths == 200
    assert result.p05_pnl_cents == -160_000


def test_payoff_distribution_reanchors_to_entry_spot() -> None:
    result = compute_hypothetical_risk(
        distribution(),
        side="call",
        strike=Decimal("120"),
        spot=Decimal("110"),
        bid=Decimal("5"),
        quote_source="nasdaq",
        quote_session=date(2026, 9, 25),
    )

    assert result.expected_pnl_cents == 36_000
    assert result.p05_pnl_cents == -170_000


def test_risk_requires_valid_distribution_and_positive_capital() -> None:
    invalid = distribution(weights=(0.2, 0.2, -0.4))
    result = compute_hypothetical_risk(
        invalid,
        side="put",
        strike=Decimal("100"),
        spot=Decimal("100"),
        bid=Decimal("4"),
        quote_source="nasdaq",
        quote_session=date(2026, 9, 25),
    )
    assert result.status == "unavailable"
    assert result.expected_pnl_cents is None

    no_capital = compute_hypothetical_risk(
        distribution(),
        side="call",
        strike=Decimal("100"),
        spot=Decimal("100"),
        bid=Decimal("100"),
        quote_source="nasdaq",
        quote_session=date(2026, 9, 25),
    )
    assert no_capital.status == "unavailable"
