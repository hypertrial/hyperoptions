"""Explicit, fail-closed horizon-band promotion from prospective forecast evidence.

The research evaluator never changes this file. An operator records a completed
as-issued holdout report here only after the predeclared gates pass. Missing or
corrupt state preserves the frozen live behavior or falls back to EWMA.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import fcntl
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any

_BANDS = {"1": range(1, 2), "2-5": range(2, 6), "6-25": range(6, 26)}
_CANDIDATES = {"empirical_scaled", "student_t_ewma", "gjr_garch_t"}
_REQUIRED_GATES = {
    "predeclared_as_issued_holdout",
    "as_issued",
    "tickers",
    "date_blocks",
    "units",
    "brier_interval",
    "log_loss",
    "subgroups",
    "availability",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _version(candidate: str) -> str:
    # Local import avoids a cycle with predictive.py during live startup.
    from stocksweeper.forecast.physical_contest import (
        EMPIRICAL_SHADOW_VERSION,
        GJR_VERSION,
        STUDENT_VERSION,
    )

    return {
        "empirical_scaled": EMPIRICAL_SHADOW_VERSION,
        "student_t_ewma": STUDENT_VERSION,
        "gjr_garch_t": GJR_VERSION,
    }[candidate]


def _band_report(report: dict[str, Any], band: str, candidate: str) -> dict[str, Any]:
    if band not in _BANDS or candidate not in _CANDIDATES:
        raise ValueError("unknown horizon band or candidate")
    if (
        report.get("source") != "append-only forecast ledger"
        or report.get("provenance") != "as_issued"
    ):
        raise ValueError("promotion requires the as-issued forecast ledger")
    result = report.get("bands", {}).get(band)
    if (
        not isinstance(result, dict)
        or result.get("band") != band
        or result.get("candidate") != candidate
    ):
        raise ValueError("report does not match horizon band and candidate")
    if result.get("period") != "holdout" or result.get("provenance") != ["as_issued"]:
        raise ValueError("promotion requires a prospective, as-issued holdout")
    try:
        date.fromisoformat(result["holdout_start"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("report needs a predeclared holdout start") from exc
    return result


def _qualifying(report: dict[str, Any], band: str, candidate: str) -> bool:
    result = _band_report(report, band, candidate)
    gates = result.get("promotion_gates")
    if not isinstance(gates, dict) or set(gates) != _REQUIRED_GATES:
        return False
    if result.get("promotion_eligible") is not True or any(
        gates[name] is not True for name in gates
    ):
        return False
    try:
        brier = result["brier"]
        log_loss = result["log_loss"]
        subgroup_ok = all(
            not item["supported"] or item["brier_delta"] <= 0.01
            for item in result["subgroups"].values()
        )
        return (
            result["tickers"] >= 20
            and result["independent_date_blocks"] >= 20
            and result["ticker_origin_horizon_units"] >= 500
            and result["contract_forecasts_available"]
            == result["baseline_contract_forecasts_available"]
            and brier["paired_delta"] < 0
            and brier["bootstrap_95"][1] < 0
            and log_loss["paired_delta"] <= 0
            and log_loss["bootstrap_95"][1] <= 0.01
            and subgroup_ok
        )
    except (KeyError, TypeError, ValueError):
        return False


def _contest_rows(
    ledger: Any,
    *,
    since: date,
    as_of: datetime,
    band: str,
    methods: tuple[str, ...],
) -> list[Any]:
    """Read actual as-issued rows once, including unavailable attempts."""
    from options_api.market_calendar import session_close
    from stocksweeper.forecast.calendar import SessionCalendar
    from stocksweeper.forecast.physical_evaluation import ContestRow

    calendar = SessionCalendar()
    rows: list[ContestRow] = []
    issues = ledger.iter_evaluation_rows(
        provenance="as_issued", since=since, issued_before=as_of,
        methods=methods, horizon_range=(_BANDS[band].start, _BANDS[band].stop - 1),
        with_crps=True, label_as_of=as_of,
    )
    for item in issues:
        origin = item["input_session"]
        method = item["method"]
        if (
            origin is None
            or method not in {"lognormal_ewma", *_CANDIDATES}
            or item["issued_at"] > as_of
        ):
            continue
        label_valid = (
            item["label_status"] == "valid"
            and item["label_checked_at"] > item["issued_at"]
            and item["label_checked_at"] <= as_of
            and item["issued_at"] < session_close(item["expiry_session"])
        )
        strike = Decimal(item["strike_exact"])
        spot = Decimal(item["spot_exact"]) if item["spot_exact"] else None
        relative = abs(float(strike / spot - 1)) if spot and spot > 0 else None
        observed = bool(item["observed_itm"]) if label_valid else None
        score = item["crps"] if label_valid else None
        rows.append(
            ContestRow(
                ticker=item["ticker"],
                origin=origin,
                expiry_session=item["expiry_session"],
                horizon=calendar.horizon(origin, item["expiration"]),
                strike=item["strike_exact"],
                side=item["side"],
                method=method,
                probability=item["itm_probability"] if item["status"] == "available" else None,
                observed_itm=observed,
                provenance="as_issued",
                moneyness=(
                    "unknown"
                    if relative is None
                    else "near_atm"
                    if relative <= 0.05
                    else "moderate"
                    if relative <= 0.15
                    else "tail"
                ),
                volatility_regime=item["volatility_regime"] or "unknown",
                event_status=item["known_event_status"] or "unknown",
                reason=item["unavailable_reason"] or item["label_reason"],
                input_vintage=item["data_hash"],
                issued_at=item["issued_at"],
                issuance_key=item["idempotency_key"],
                contract_id=item["contract_key"],
                crps=score,
                prepare_ms=item["prepare_ms"],
                lookup_ms=item["lookup_ms"],
            )
        )
    return rows


def _ledger_report(
    rows: list[Any], band: str, candidate: str, holdout_start: date
) -> dict[str, Any]:
    """Recompute the recorded as-issued holdout; never trust imported JSON."""
    from stocksweeper.forecast.calendar import SessionCalendar
    from stocksweeper.forecast.physical_evaluation import evaluate_band

    result = evaluate_band(
        rows,
        candidate,
        band,
        holdout_start=holdout_start,
        period="holdout",
        calendar=SessionCalendar(),
    )
    return {
        "source": "append-only forecast ledger",
        "provenance": "as_issued",
        "bands": {band: result},
    }


def _locked(action: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(action)
    def run(self: PromotionRegistry, *args: Any, **kwargs: Any) -> Any:
        with self._exclusive():
            return action(self, *args, **kwargs)

    return run


class PromotionRegistry:
    """Small atomic local artifact; no automatic promotion from retrospective runs."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "forecast_champions.json"
        self._stamp: tuple[int, int, int] | None = None
        self._state: dict[str, Any] | None = None

    @contextmanager
    def _exclusive(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.data_dir / ".forecast-champions.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self._stamp = None
            self._state = None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._stamp = None
            self._state = None
            return None
        except OSError:
            self._state = {}
            return self._state
        stamp = stat.st_ino, stat.st_mtime_ns, stat.st_size
        if self._stamp == stamp:
            return self._state
        self._stamp = stamp
        try:
            state = json.loads(self.path.read_text())
            if state.get("schema_version") != 1 or not isinstance(state.get("bands"), dict):
                raise ValueError("invalid promotion state")
        except (OSError, ValueError, AttributeError):
            self._state = {}
            return self._state
        self._state = state
        return state

    def method(self, horizon: int) -> str | None:
        """None keeps frozen selection; EWMA is an explicit rollback/fail-safe."""
        band = next((name for name, sessions in _BANDS.items() if horizon in sessions), None)
        if band is None:
            return "lognormal_ewma" if horizon > 25 else None
        state = self._read()
        if state is None:
            return None
        if not state:
            return "lognormal_ewma"
        choice = state["bands"].get(band)
        if choice is None:
            return None
        if not isinstance(choice, dict):
            return "lognormal_ewma"
        if choice.get("status") == "predeclared":
            return "lognormal_ewma" if choice.get("baseline_during_holdout") else None
        if choice.get("status") != "active":
            return "lognormal_ewma"
        candidate = choice.get("candidate")
        try:
            if (
                candidate not in _CANDIDATES
                or choice.get("model_version") != _version(candidate)
                or choice.get("report_digest") != _digest(choice["report"])
                or not _qualifying(choice["report"], band, candidate)
                or choice["report"]["bands"][band]["holdout_start"] != choice["holdout_start"]
                or datetime.fromisoformat(choice["declared_at"]).date()
                >= date.fromisoformat(choice["holdout_start"])
            ):
                return "lognormal_ewma"
        except (KeyError, TypeError, ValueError):
            return "lognormal_ewma"
        return candidate

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stat = self.path.stat()
            current = stat.st_ino, stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            current = None
        if current != self._stamp:
            raise RuntimeError("promotion state changed during evaluation; retry")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".forecast-champions-", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(state, output, sort_keys=True, indent=2, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._stamp = None

    @_locked
    def predeclare(
        self, band: str, candidate: str, holdout_start: date, *, now: datetime | None = None
    ) -> None:
        if band not in _BANDS or candidate not in _CANDIDATES:
            raise ValueError("unknown horizon band or candidate")
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("predeclaration time must be timezone-aware")
        if holdout_start <= now.astimezone(UTC).date():
            raise ValueError("holdout must be declared before any origin in it")
        state = self._read() or {"schema_version": 1, "bands": {}}
        if self.path.exists() and not self._state:
            raise ValueError("corrupt promotion state must be repaired before predeclaration")
        previous = state["bands"].get(band)
        if previous is not None and (
            not isinstance(previous, dict) or previous.get("status") != "rolled_back"
        ):
            raise ValueError("horizon band already has a promotion decision")
        if previous is not None:
            state.setdefault("past_decisions", []).append({"band": band, **previous})
        state["bands"][band] = {
            "status": "predeclared",
            "candidate": candidate,
            "model_version": _version(candidate),
            "holdout_start": holdout_start.isoformat(),
            "declared_at": now.astimezone(UTC).isoformat(),
            "baseline_during_holdout": previous is not None,
        }
        self._write(state)

    @_locked
    def promote(self, band: str, *, now: datetime | None = None) -> None:
        from stocksweeper.forecast.ledger import ForecastLedger

        state = self._read()
        choice = state.get("bands", {}).get(band) if state else None
        if not choice or choice.get("status") != "predeclared":
            raise ValueError("horizon band needs an earlier predeclared holdout")
        candidate = choice["candidate"]
        holdout_start = date.fromisoformat(choice["holdout_start"])
        if choice["model_version"] != _version(candidate):
            raise ValueError("model version changed after holdout predeclaration")
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("promotion time must be timezone-aware")
        if now.astimezone(UTC).date() <= holdout_start:
            raise ValueError("holdout cannot be promoted before its outcomes mature")
        report = _ledger_report(
            _contest_rows(
                ForecastLedger(self.data_dir), since=holdout_start, as_of=now,
                band=band, methods=("lognormal_ewma", candidate),
            ),
            band,
            candidate,
            holdout_start,
        )
        if not _qualifying(report, band, candidate):
            raise ValueError("prospective as-issued holdout did not pass promotion gates")
        choice.update(
            status="active",
            promoted_at=now.astimezone(UTC).isoformat(),
            report=report,
            report_digest=_digest(report),
        )
        self._write(state)

    @_locked
    def post_release_check(self, ledger: Any, *, as_of: datetime | None = None) -> dict[str, str]:
        """Audit active bands once daily; return failures to EWMA."""
        now = as_of or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("quality-check time must be timezone-aware")
        state = self._read()
        if not state:
            return {}
        active = {
            band: choice
            for band, choice in state["bands"].items()
            if isinstance(choice, dict) and choice.get("status") == "active"
        }
        today = now.astimezone(UTC).date().isoformat()
        due = {
            band: choice
            for band, choice in active.items()
            if choice.get("last_quality_date") != today
        }
        if not due:
            return {band: "already_checked" for band in active}
        results = {band: "already_checked" for band in active if band not in due}
        for band, choice in due.items():
            promoted_on = datetime.fromisoformat(choice["promoted_at"]).date()
            promoted_at = datetime.fromisoformat(choice["promoted_at"])
            candidate = choice["candidate"]
            holdout_start = date.fromisoformat(choice["holdout_start"])
            rows = []  # Release the previous band's cohort before reading another.
            rows = _contest_rows(
                ledger, since=holdout_start, as_of=now, band=band,
                methods=("lognormal_ewma", candidate),
            )
            original_report = _ledger_report(
                [row for row in rows if row.issued_at is not None and row.issued_at <= promoted_at],
                band,
                candidate,
                holdout_start,
            )
            if not _qualifying(original_report, band, candidate):
                choice.update(
                    status="rolled_back",
                    last_quality_date=today,
                    rollback_at=now.astimezone(UTC).isoformat(),
                    rollback_reason="holdout_evidence_invalidated",
                    rollback_report=original_report,
                    rollback_report_digest=_digest(original_report),
                )
                results[band] = "rolled_back_to_ewma"
                continue
            report = _ledger_report(
                [row for row in rows if row.origin >= promoted_on + timedelta(days=1)],
                band,
                candidate,
                promoted_on + timedelta(days=1),
            )
            result = _band_report(report, band, candidate)
            outcome = self._quality_outcome(result)
            choice["last_quality_date"] = today
            choice["last_quality_report_digest"] = _digest(report)
            if outcome == "rolled_back_to_ewma":
                choice.update(
                    status="rolled_back",
                    rollback_at=now.astimezone(UTC).isoformat(),
                    rollback_report=report,
                    rollback_report_digest=_digest(report),
                )
            results[band] = outcome
        self._write(state)
        return results

    @staticmethod
    def _quality_outcome(result: dict[str, Any]) -> str:
        baseline_count = result.get("baseline_contract_forecasts_available", 0)
        candidate_count = result.get("contract_forecasts_available", 0)
        enough = (
            result.get("tickers", 0) >= 20
            and result.get("independent_date_blocks", 0) >= 20
            and result.get("ticker_origin_horizon_units", 0) >= 500
        )
        availability_loss = baseline_count >= 500 and candidate_count < baseline_count
        if not enough and not availability_loss:
            return "insufficient_evidence"
        brier = result.get("brier", {})
        log_loss = result.get("log_loss", {})
        brier_interval = brier.get("bootstrap_95")
        log_interval = log_loss.get("bootstrap_95")
        subgroup_loss = any(
            item.get("supported") and item.get("brier_delta", 0) > 0.01
            for item in result.get("subgroups", {}).values()
        )
        failed = (
            availability_loss
            or brier.get("paired_delta") is None
            or brier["paired_delta"] >= 0
            or brier_interval is None
            or brier_interval[1] >= 0
            or log_loss.get("paired_delta") is None
            or log_loss["paired_delta"] > 0
            or log_interval is None
            or log_interval[1] > 0.01
            or subgroup_loss
        )
        return "rolled_back_to_ewma" if failed else "passing"


def main() -> None:
    """Explicit operator action; evaluating reports alone never activates a model."""
    import argparse

    from stocksweeper.config import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    declare = actions.add_parser("predeclare")
    declare.add_argument("band", choices=tuple(_BANDS))
    declare.add_argument("candidate", choices=tuple(sorted(_CANDIDATES)))
    declare.add_argument("holdout_start", type=date.fromisoformat)
    activate = actions.add_parser("promote")
    activate.add_argument("band", choices=tuple(_BANDS))
    actions.add_parser("status")
    args = parser.parse_args()
    registry = PromotionRegistry(args.data_dir or load_settings().resolved_data_dir())
    try:
        if args.action == "predeclare":
            registry.predeclare(args.band, args.candidate, args.holdout_start)
        elif args.action == "promote":
            registry.promote(args.band)
        else:
            state = registry._read() or {"bands": {}}
            print(
                json.dumps(
                    {
                        band: {
                            key: value
                            for key, value in choice.items()
                            if key
                            in {
                                "status",
                                "candidate",
                                "model_version",
                                "holdout_start",
                                "promoted_at",
                                "rollback_at",
                            }
                        }
                        for band, choice in state["bands"].items()
                    },
                    sort_keys=True,
                    indent=2,
                )
            )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
