from datetime import UTC, date, datetime, timedelta

import pytest

from options_api.outcomes import TERMS_NOTE
from stocksweeper.forecast.ledger import ForecastLabel, ForecastLedger
from stocksweeper.storage.db import connect


NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


def _contracts(ledger, expiry, names, *, terms=TERMS_NOTE):
    keys = [f"w1:TEST:TEST:call:{expiry}:{name}.000" for name in names]
    with connect(ledger.path) as db:
        db.executemany(
            """INSERT INTO forecast_issuances
               (idempotency_key, contract_key, ticker, root, side, expiration,
                expiry_session, strike_exact, terms_note, contract_since,
                input_session, issued_at, status, provenance)
               VALUES (?, ?, 'TEST', 'TEST', 'call', ?, ?, ?, ?, ?, ?, ?,
                       'unavailable', 'as_issued')""",
            [
                (
                    key + terms,
                    key,
                    expiry,
                    expiry,
                    f"{name}.000",
                    terms,
                    expiry - timedelta(days=4),
                    expiry - timedelta(days=4),
                    NOW - timedelta(days=40),
                )
                for key, name in zip(keys, names, strict=True)
            ],
        )
    return keys


def _checked(ledger, key, expiry, checked_at, *, status="pending", terms=TERMS_NOTE):
    ledger.record_label(
        ForecastLabel(
            contract_key=key,
            terms_note=terms,
            expiry_session=expiry,
            checked_at=checked_at,
            status=status,
            reason=None if status == "valid" else "nasdaq_close_missing",
            source="Nasdaq historical Close (Yahoo cross-check)" if status == "valid" else None,
            nasdaq_close_exact="100" if status == "valid" else None,
            yahoo_close_exact="100" if status == "valid" else None,
            selected_close_exact="100" if status == "valid" else None,
            classification="atm" if status == "valid" else None,
        )
    )


def test_backlog_and_advancing_maturities_do_not_starve_retries(tmp_path):
    ledger = ForecastLedger(tmp_path)
    old_expiry = date(2026, 9, 4)
    old = _contracts(ledger, old_expiry, range(1, 602))
    with connect(ledger.path) as db:
        db.executemany(
            """INSERT INTO forecast_labels
               (idempotency_key, contract_key, terms_note, expiry_session,
                checked_at, status, reason) VALUES (?, ?, ?, ?, ?, 'pending', ? )""",
            [
                (key, key, TERMS_NOTE, old_expiry, NOW - timedelta(hours=2), "nasdaq_close_missing")
                for key in old
            ],
        )
    new = _contracts(ledger, date(2026, 9, 25), [700])[0]
    seen = set()
    retries = set()
    for tick in range(25):
        clock = NOW + timedelta(minutes=5 * tick)
        # New arrivals cannot indefinitely displace overdue retries either.
        _contracts(ledger, date(2026, 9, 29), [800 + tick])
        batch = ledger.due_label_contracts(clock, limit=50)
        for item in batch:
            key = item["contract_key"]
            if key in seen:
                retries.add(key)
            seen.add(key)
        with connect(ledger.path) as db:
            db.executemany(
                """INSERT INTO forecast_labels
                   (idempotency_key, contract_key, terms_note, expiry_session,
                    checked_at, status, reason) VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                [
                    (
                        f"{tick}:{item['contract_key']}",
                        item["contract_key"],
                        TERMS_NOTE,
                        item["expiry_session"],
                        clock,
                        "nasdaq_close_missing",
                    )
                    for item in batch
                ],
            )
    assert new in seen
    assert set(old) <= seen
    assert retries & set(old)


@pytest.mark.parametrize(
    "status,interval",
    [
        ("pending", timedelta(hours=1)),
        ("excluded", timedelta(hours=1)),
        ("valid", timedelta(days=1)),
    ],
)
def test_retry_boundary_and_latest_revision(tmp_path, status, interval):
    ledger = ForecastLedger(tmp_path)
    expiry = date(2026, 9, 25)
    key = _contracts(ledger, expiry, [100])[0]
    _checked(ledger, key, expiry, NOW - timedelta(days=2))
    _checked(ledger, key, expiry, NOW - interval, status=status)
    assert ledger.due_label_contracts(NOW - timedelta(microseconds=1)) == []
    assert [row["contract_key"] for row in ledger.due_label_contracts(NOW)] == [key]


def test_oldest_eligibility_precedes_expiry_and_ties_are_deterministic(tmp_path):
    ledger = ForecastLedger(tmp_path)
    early, later = date(2026, 9, 4), date(2026, 9, 25)
    a = _contracts(ledger, early, [100])[0]
    b = _contracts(ledger, later, [200])[0]
    _checked(ledger, a, early, NOW - timedelta(hours=2))
    _checked(ledger, b, later, NOW - timedelta(hours=3))
    assert [row["contract_key"] for row in ledger.due_label_contracts(NOW)] == [b, a]
    _contracts(ledger, later, [300], terms="z-terms")
    _contracts(ledger, later, [300], terms="a-terms")
    _contracts(ledger, date(2026, 10, 2), [400])
    batch = ledger.due_label_contracts(NOW)
    assert [row["terms_note"] for row in batch[:2]] == ["a-terms", "z-terms"]
    assert len(batch) == 4
