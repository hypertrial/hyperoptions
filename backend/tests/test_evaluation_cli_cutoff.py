import importlib.util
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from options_api.outcomes import TERMS_NOTE
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.evidence_reports import ledger_contest
from stocksweeper.forecast.ledger import ForecastIssuance, ForecastLabel, ForecastLedger
from stocksweeper.forecast.predictive import BASELINE_VERSION
from stocksweeper.forecast.physical_contest import STUDENT_VERSION

SCRIPT = Path(__file__).parents[1] / "scripts" / "evaluate_predictive.py"


def fixture(root, *, checked_at=None):
    issued = datetime(2026, 9, 25, 22, tzinfo=UTC)
    expiry = date(2026, 9, 28)
    contract = "w1:ACME:ACME:call:2026-09-28:100.000"
    ledger = ForecastLedger(root)
    digest = ledger.record_distribution((90.0, 110.0), (0.5, 0.5), issued)
    for method, version in [
        ("lognormal_ewma", BASELINE_VERSION),
        ("student_t_ewma", STUDENT_VERSION),
    ]:
        ledger.record(
            ForecastIssuance(
                contract,
                "ACME",
                "ACME",
                "call",
                expiry,
                expiry,
                "100.000",
                TERMS_NOTE,
                date(2026, 9, 25),
                date(2026, 9, 25),
                issued,
                issued,
                version,
                method,
                "a" * 64,
                digest,
                "completed_close",
                "100",
                "available",
                0.5,
                0.5,
                0.0,
                None,
            )
        )
    ledger.record_label(
        ForecastLabel(
            contract,
            TERMS_NOTE,
            expiry,
            checked_at or datetime(2026, 9, 29, 22, tzinfo=UTC),
            "valid",
            None,
            "Nasdaq historical Close (Yahoo cross-check)",
            "110",
            "110",
            "110",
            "itm",
        )
    )
    return ledger


def load_script():
    spec = importlib.util.spec_from_file_location("evaluate_predictive_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ledger_contest_cli_respects_asof(tmp_path, monkeypatch, capsys):
    fixture(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            str(SCRIPT),
            "--ledger-contest",
            "--candidate",
            "student_t_ewma",
            "--data-dir",
            str(tmp_path),
            "--as-of",
            "2026-09-25",
        ],
    )
    load_script().main()
    report = json.loads(capsys.readouterr().out)
    assert report["bands"]["1"]["ticker_origin_horizon_units"] == 0, (
        "A Sep25 report cannot score a Sep28 outcome checked on Sep29"
    )


def test_same_ledger_direct_cutoff_is_honored(tmp_path):
    ledger = fixture(tmp_path)
    report = ledger_contest(
        ledger,
        SessionCalendar(),
        "as_issued",
        "student_t_ewma",
        None,
        "all",
        as_of=datetime(2026, 9, 25, 23, tzinfo=UTC),
    )
    assert report["bands"]["1"]["ticker_origin_horizon_units"] == 0


def test_current_ledger_cli_control_scores_matured_outcome(tmp_path, monkeypatch, capsys):
    fixture(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            str(SCRIPT),
            "--ledger-contest",
            "--candidate",
            "student_t_ewma",
            "--data-dir",
            str(tmp_path),
        ],
    )
    load_script().main()
    report = json.loads(capsys.readouterr().out)
    assert report["bands"]["1"]["ticker_origin_horizon_units"] == 1


@pytest.mark.parametrize(
    "day, utc_end",
    [
        ("2026-01-05", "2026-01-06T04:59:59.999999+00:00"),
        ("2026-09-25", "2026-09-26T03:59:59.999999+00:00"),
    ],
)
def test_cli_uses_new_york_end_of_date(day, utc_end, tmp_path, monkeypatch, capsys):
    module = load_script()

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 22, tzinfo=UTC)

    monkeypatch.setattr(module, "datetime", FixedClock)

    def contest(*args, as_of):
        assert as_of.astimezone(UTC).isoformat() == utc_end
        return {}

    monkeypatch.setattr(module, "ledger_contest", contest)
    monkeypatch.setattr(
        "sys.argv",
        [
            str(SCRIPT),
            "--ledger-contest",
            "--candidate",
            "student_t_ewma",
            "--data-dir",
            str(tmp_path),
            "--as-of",
            day,
        ],
    )
    module.main()
    capsys.readouterr()


@pytest.mark.parametrize(
    "mode, day, message",
    [
        ("--replay-cohort", "2026-09-25", "--as-of"),
        ("--ledger-contest", "2999-01-01", "completed"),
    ],
)
def test_invalid_cutoff_rejected_before_data_access(
    mode, day, message, tmp_path, monkeypatch, capsys
):
    module = load_script()

    def no_access(*args, **kwargs):
        pytest.fail("invalid arguments must not access research data")

    monkeypatch.setattr(module, "ForecastLedger", no_access)
    monkeypatch.setattr(module, "_replay_cohort", no_access)
    monkeypatch.setattr(
        "sys.argv",
        [
            str(SCRIPT),
            mode,
            "--candidate",
            "student_t_ewma",
            "--data-dir",
            str(tmp_path),
            "--as-of",
            day,
        ],
    )
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_includes_same_ny_day_label_but_excludes_later_revision(tmp_path, monkeypatch, capsys):
    # Just before NY midnight is already the following UTC date.
    ledger = fixture(tmp_path, checked_at=datetime(2026, 9, 29, 3, 59, 59, tzinfo=UTC))
    ledger.record_label(
        ForecastLabel(
            "w1:ACME:ACME:call:2026-09-28:100.000",
            TERMS_NOTE,
            date(2026, 9, 28),
            datetime(2026, 9, 29, 4, tzinfo=UTC),
            "excluded",
            "later source conflict",
            None,
            None,
            None,
            None,
            None,
        )
    )
    argv = [
        str(SCRIPT),
        "--ledger-contest",
        "--candidate",
        "student_t_ewma",
        "--data-dir",
        str(tmp_path),
    ]
    monkeypatch.setattr("sys.argv", [*argv, "--as-of", "2026-09-28"])
    load_script().main()
    historical = json.loads(capsys.readouterr().out)
    assert historical["bands"]["1"]["ticker_origin_horizon_units"] == 1
    monkeypatch.setattr("sys.argv", argv)
    load_script().main()
    current = json.loads(capsys.readouterr().out)
    assert current["bands"]["1"]["ticker_origin_horizon_units"] == 0
