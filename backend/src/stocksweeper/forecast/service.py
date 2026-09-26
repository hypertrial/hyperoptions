"""Prepare a frozen peer cohort and publish only audited option probabilities."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import isfinite
from pathlib import Path

import polars as pl

from stocksweeper.forecast.calibration import fit_audit, tail_probability
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.market import (
    ForecastPriceStore,
    ForecastProvider,
    YahooForecastProvider,
    clean_completed,
    price_hash,
)
from stocksweeper.forecast.models import ForecastSnapshot, PeerCandidate, Side, State
from stocksweeper.forecast.repository import ForecastRepository
from stocksweeper.forecast.samples import (
    MAX_HORIZON,
    current_state_and_volatility,
    observations,
    standardized_strike,
)
from stocksweeper.forecast.selection import (
    EVIDENCE_BARS,
    PREFIX_BARS,
    Selection,
    catalog,
    select_strategy,
    strategy_states,
)
from stocksweeper.strategy.model import Strategy

logger = logging.getLogger(__name__)
FIT_START, FIT_END = date(2021, 1, 1), date(2022, 12, 31)
AUDIT_START, AUDIT_END = date(2023, 1, 1), date(2025, 12, 31)
PEER_CANDIDATE_LIMIT = 300
PEER_LIMIT = 100
# Every fourth member is held out: 67 members yield 50 fit and 17 audit peers.
MIN_COHORT = 67
MODEL_MAX_AGE_SESSIONS = 90
TRANSIENT_RETRY_DELAY = timedelta(minutes=30)
Progress = Callable[[float, str], None]


def _report(progress: Progress | None, fraction: float, message: str) -> None:
    if progress is not None:
        progress(fraction, message)


def _retry_due(payload: dict, *, force: bool) -> bool:
    if payload.get("reason") not in {"peer_data_missing", "market_data_missing"}:
        return False
    if force:
        return True
    attempted = payload.get("attempted_at")
    if not isinstance(attempted, str):
        return True
    try:
        previous = datetime.fromisoformat(attempted)
    except ValueError:
        return True
    return previous.tzinfo is None or datetime.now(UTC) >= previous + TRANSIENT_RETRY_DELAY


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _audit_tickers(members: Sequence[dict]) -> set[str]:
    """Hold out roughly a quarter of each sector, with exact global count."""
    groups: dict[str, list[str]] = defaultdict(list)
    for member in members:
        groups[member["sector"]].append(member["ticker"])
    total = len(members)
    if total == 0:
        return set()
    audit_count = max(1, round(total / 4))
    quotas = {sector: len(tickers) * audit_count // total for sector, tickers in groups.items()}
    remainders = sorted(
        groups,
        key=lambda sector: (-(len(groups[sector]) * audit_count % total), sector),
    )
    for sector in remainders[: audit_count - sum(quotas.values())]:
        quotas[sector] += 1
    return {
        ticker
        for sector, tickers in groups.items()
        for ticker in sorted(tickers, key=lambda name: (_digest([7, name]), name))[: quotas[sector]]
    }


def _selection_payload(selection: Selection) -> dict[str, object]:
    return {
        "strategy": selection.strategy.model_dump(mode="json"),
        "cutoff": selection.cutoff.isoformat(),
        "prefix_hash": selection.prefix_hash,
        "catalog_hash": selection.catalog_hash,
        "validation_sharpe": selection.validation_sharpe,
        "robustness": selection.robustness,
    }


def _selection_from_payload(payload: dict) -> Selection:
    return Selection(
        strategy=Strategy.model_validate(payload["strategy"]),
        cutoff=date.fromisoformat(payload["cutoff"]),
        prefix_hash=payload["prefix_hash"],
        catalog_hash=payload["catalog_hash"],
        validation_sharpe=float(payload["validation_sharpe"]),
        robustness=float(payload["robustness"]),
    )


class ForecastService:
    """Single-process forecast coordinator; all provider calls stay outside DB locks."""

    def __init__(self, data_dir: Path, provider: ForecastProvider | None = None) -> None:
        self.data_dir = data_dir
        self.provider = provider or YahooForecastProvider()
        self.prices = ForecastPriceStore(data_dir, self.provider)
        self.repository = ForecastRepository(data_dir)
        self.calendar = SessionCalendar()
        self._guard = threading.RLock()
        self._cohort_reason = "cohort_insufficient"

    def initialize(self) -> None:
        self.repository.initialize()

    def latest_snapshot(
        self, ticker: str, side: Side, strike: Decimal, expiry: date
    ) -> ForecastSnapshot | None:
        return self.repository.latest_snapshot(ticker, side, strike, expiry)

    def last_available_snapshot(
        self, ticker: str, side: Side, strike: Decimal, expiry: date
    ) -> ForecastSnapshot | None:
        return self.repository.last_available_snapshot(ticker, side, strike, expiry)

    def refresh_contract(
        self,
        ticker: str,
        side: Side,
        strike: Decimal,
        expiry: date,
        *,
        as_of: datetime,
        candidates: Sequence[PeerCandidate],
        progress: Progress | None = None,
        force_rebuild: bool = False,
        watched_at: datetime | None = None,
    ) -> ForecastSnapshot:
        """Save a daily snapshot, including a specific unavailable reason on failure."""
        if side not in ("call", "put") or not strike.is_finite() or strike <= 0:
            raise ValueError("invalid option side or strike")
        ticker = ticker.upper()
        completed = self.calendar.last_completed(as_of)
        horizon = self.calendar.horizon(completed, expiry)
        with self._guard:
            try:
                _report(progress, 0.01, "Checking forecast inputs")
                result = self._refresh(
                    ticker,
                    side,
                    strike,
                    expiry,
                    completed,
                    horizon,
                    candidates,
                    progress,
                    force_rebuild,
                    watched_at,
                )
            except Exception:
                logger.exception("forecast refresh failed for %s", ticker)
                result = self._unavailable(
                    ticker, side, strike, expiry, completed, "model_not_ready"
                )
            self.repository.save_snapshot(result)
            _report(progress, 1.0, "Forecast snapshot saved")
            return result

    def _refresh(
        self,
        ticker: str,
        side: Side,
        strike: Decimal,
        expiry: date,
        completed: date,
        horizon: int,
        candidates: Sequence[PeerCandidate],
        progress: Progress | None = None,
        force_rebuild: bool = False,
        watched_at: datetime | None = None,
    ) -> ForecastSnapshot:
        if horizon <= 0:
            return self._unavailable(ticker, side, strike, expiry, completed, "expiry_completed")
        if horizon > MAX_HORIZON:
            return self._unavailable(ticker, side, strike, expiry, completed, "horizon_unsupported")
        if completed < self.calendar.expiry_session(AUDIT_END):
            return self._unavailable(
                ticker, side, strike, expiry, completed, "audit_window_incomplete"
            )
        if watched_at is not None and watched_at.tzinfo is None:
            raise ValueError("watched_at must have a timezone")

        try:
            fetched = self.prices.update(ticker, completed)
        except (ValueError, OSError):
            logger.exception("forecast target prices unavailable for %s", ticker)
            return self._unavailable(ticker, side, strike, expiry, completed, "market_data_missing")
        if watched_at is not None:
            watched_actions = fetched.filter(
                (pl.col("ts") >= watched_at.date()) & (pl.col("ts") <= completed)
            )
            if watched_actions.filter(
                pl.col("stock_splits").is_null()
                | ~pl.col("stock_splits").is_finite()
                | (pl.col("stock_splits") < 0)
            ).height:
                return self._unavailable(
                    ticker, side, strike, expiry, completed, "contract_terms_ambiguous"
                )
            if watched_actions.filter(pl.col("stock_splits") > 0).height:
                return self._unavailable(
                    ticker, side, strike, expiry, completed, "contract_terms_changed"
                )
        bars = clean_completed(fetched, completed, self.calendar)
        if bars.height < PREFIX_BARS + EVIDENCE_BARS:
            return self._unavailable(
                ticker, side, strike, expiry, completed, "ticker_history_short"
            )
        if bars["ts"][-1] != completed:
            return self._unavailable(ticker, side, strike, expiry, completed, "market_data_missing")
        _report(progress, 0.05, "Selecting target strategy")
        selection, reason = self._selection(ticker, bars, completed, peer=False)
        if selection is None:
            return self._unavailable(
                ticker, side, strike, expiry, completed, reason or "ticker_strategy_unqualified"
            )
        states = strategy_states(bars, selection.strategy)
        current = current_state_and_volatility(bars, states, completed, self.calendar)
        if current is None:
            return self._unavailable(ticker, side, strike, expiry, completed, "market_data_missing")
        state, volatility, spot = current
        _report(progress, 0.1, "Preparing peer cohort")

        cohort = self._cohort(completed, candidates, progress, force_rebuild)
        if cohort is None:
            return self._unavailable(
                ticker,
                side,
                strike,
                expiry,
                completed,
                self._cohort_reason,
                selection=selection,
                state=state,
            )
        # Calibration on the watched ticker would make the holdout audit non-independent.
        if ticker in {member["ticker"] for member in cohort["members"]}:
            return self._unavailable(
                ticker,
                side,
                strike,
                expiry,
                completed,
                "ticker_in_calibration_cohort",
                selection=selection,
                state=state,
            )

        model, model_reason = self._active_model(
            completed, horizon, cohort, progress, force_rebuild
        )
        if model is None:
            return self._unavailable(
                ticker,
                side,
                strike,
                expiry,
                completed,
                model_reason,
                selection=selection,
                state=state,
            )
        evidence = model["states"][state]
        if not evidence["qualified"]:
            return self._unavailable(
                ticker,
                side,
                strike,
                expiry,
                completed,
                evidence["reason"],
                selection=selection,
                state=state,
                model=model,
                evidence=evidence,
            )
        threshold = standardized_strike(strike, spot, volatility, horizon)
        if not isfinite(threshold) or not (
            evidence["support_low"] <= threshold <= evidence["support_high"]
        ):
            return self._unavailable(
                ticker,
                side,
                strike,
                expiry,
                completed,
                "strike_outside_support",
                selection=selection,
                state=state,
                model=model,
                evidence=evidence,
            )
        probability = tail_probability(evidence["fit_values"], threshold, side)
        _report(progress, 0.98, "Computing audited ITM probability")
        return ForecastSnapshot(
            ticker=ticker,
            side=side,
            strike=strike,
            expiry=expiry,
            as_of=completed,
            status="available",
            reason=None,
            itm_probability=probability,
            model_id=model["model_id"],
            data_hash=_digest([model["data_hash"], price_hash(bars)]),
            strategy_id=selection.strategy.id,
            strategy_name=selection.strategy.name,
            signal_state=state,
            fit_peers=evidence["fit_peers"],
            audit_peers=evidence["audit_peers"],
            audit_blocks=evidence["audit_blocks"],
            cohort_size=len(cohort["members"]),
            fit_samples=evidence["fit_samples"],
            audit_samples=evidence["audit_samples"],
            crps_skill_lower_90=evidence["crps_skill_lower_90"],
            brier_delta=evidence["brier_delta"],
        )

    def _selection(
        self, ticker: str, bars, completed: date, *, peer: bool
    ) -> tuple[Selection | None, str | None]:
        minimum = PREFIX_BARS + (0 if peer else EVIDENCE_BARS)
        if bars.height < minimum:
            return None, "peer_history_short" if peer else "ticker_history_short"
        prefix = bars.head(PREFIX_BARS)
        if peer and prefix["ts"][-1] > date(2020, 12, 31):
            return None, "peer_history_short"
        prefix_hash = price_hash(prefix)
        _, catalog_hash = catalog()
        saved = self.repository.get_selection(ticker, prefix_hash, catalog_hash)
        if saved:
            return _selection_from_payload(saved), None
        selection, reason = select_strategy(
            ticker, bars, completed, self.data_dir, peer=peer, calendar=self.calendar
        )
        if selection:
            self.repository.save_selection(
                ticker, prefix_hash, catalog_hash, _selection_payload(selection)
            )
        return selection, reason

    def _cohort(
        self,
        completed: date,
        candidates: Sequence[PeerCandidate],
        progress: Progress | None = None,
        force_rebuild: bool = False,
    ) -> dict | None:
        saved = self.repository.get_cohort()
        if saved is not None:
            _, current_hash = catalog()
            if saved["catalog_hash"] != current_hash:
                _report(progress, 0.65, "Requalifying frozen peers for updated engine")
                return saved
            _report(progress, 0.65, "Using frozen peer cohort")
            return saved
        month = completed.strftime("%Y-%m")
        _, catalog_hash = catalog()
        unique: dict[str, PeerCandidate] = {}
        for candidate in sorted(
            candidates, key=lambda item: (item.ticker.upper(), item.sector or "")
        ):
            ticker = candidate.ticker.upper()
            if ticker.isascii() and ticker.replace(".", "").replace("-", "").isalnum():
                unique.setdefault(ticker, PeerCandidate(ticker=ticker, sector=candidate.sector))
        ordered = sorted(unique.values(), key=lambda item: (_digest([7, item.ticker]), item.ticker))
        ordered = ordered[:PEER_CANDIDATE_LIMIT]
        candidate_hash = _digest([item.model_dump() for item in ordered])
        attempt = self.repository.get_cohort_attempt(month, candidate_hash, catalog_hash)
        if attempt and not _retry_due(attempt, force=force_rebuild):
            self._cohort_reason = attempt.get("reason", "cohort_insufficient")
            return None
        sectors: dict[str, deque[PeerCandidate]] = defaultdict(deque)
        sector_errors = 0
        for index, item in enumerate(ordered):
            sector = item.sector.strip() if item.sector else None
            if sector:
                sectors[sector].append(item)
            else:
                sector_errors += 1
            if index % 10 == 0:
                _report(
                    progress,
                    0.1 + 0.1 * (index + 1) / max(1, len(ordered)),
                    f"Classifying Nasdaq peers {index + 1}/{len(ordered)}",
                )
        round_robin: list[tuple[str, PeerCandidate]] = []
        while any(sectors.values()):
            for sector in sorted(sectors):
                if sectors[sector]:
                    round_robin.append((sector, sectors[sector].popleft()))
        members: list[dict] = []
        provider_errors = sector_errors
        for index, (sector, item) in enumerate(round_robin):
            if len(members) == PEER_LIMIT:
                break
            if index % 5 == 0:
                _report(
                    progress,
                    0.2 + 0.45 * (index + 1) / max(1, len(round_robin)),
                    f"Qualifying Nasdaq peers {index + 1}/{len(round_robin)}",
                )
            try:
                bars = clean_completed(
                    self.prices.update(item.ticker, AUDIT_END), AUDIT_END, self.calendar
                )
                selection, _ = self._selection(item.ticker, bars, AUDIT_END, peer=True)
            except Exception:
                logger.exception("forecast peer qualification failed for %s", item.ticker)
                provider_errors += 1
                continue
            if selection is not None:
                members.append(
                    {
                        "ticker": item.ticker,
                        "sector": sector,
                        "selection": _selection_payload(selection),
                        "qualification_data_hash": price_hash(bars),
                    }
                )
        if len(members) < MIN_COHORT:
            self._cohort_reason = "peer_data_missing" if provider_errors else "cohort_insufficient"
            self.repository.save_cohort_attempt(
                month,
                candidate_hash,
                catalog_hash,
                {
                    "reason": self._cohort_reason,
                    "qualified_count": len(members),
                    "candidate_count": len(ordered),
                    "provider_errors": provider_errors,
                    "attempted_through": AUDIT_END.isoformat(),
                    "attempted_at": datetime.now(UTC).isoformat(),
                },
            )
            return None
        payload = {
            "members": members,
            "candidate_hash": candidate_hash,
            "catalog_hash": catalog_hash,
            "candidate_count": len(ordered),
            "qualification_through": AUDIT_END.isoformat(),
            "created_session": completed.isoformat(),
            "sector_counts": {
                sector: sum(m["sector"] == sector for m in members)
                for sector in sorted({m["sector"] for m in members})
            },
        }
        self.repository.save_cohort(candidate_hash, catalog_hash, payload)
        _report(progress, 0.65, f"Frozen cohort: {len(members)} qualified peers")
        return self.repository.get_cohort()

    def _active_model(
        self,
        completed: date,
        horizon: int,
        cohort: dict,
        progress: Progress | None = None,
        force_rebuild: bool = False,
    ) -> tuple[dict | None, str]:
        month = completed.strftime("%Y-%m")
        version_id = cohort["version_id"]
        _, catalog_hash = catalog()
        build = self.repository.get_build(month, horizon, version_id, catalog_hash)
        retry = build is not None and _retry_due(build, force=force_rebuild)
        if build is None or retry:
            build = self._build_model(completed, horizon, cohort, progress)
            pending_cohort = build.pop("_pending_cohort", None)
            version_id = build.get("cohort_version_id", version_id)
            build["cohort_version_id"] = version_id
            model_id = self.repository.save_build(month, horizon, version_id, catalog_hash, build)
            if pending_cohort is not None:
                # The audited model becomes durable before its cohort version
                # is exposed to subsequent refreshes.
                saved_version = self.repository.save_cohort_version(pending_cohort)
                if saved_version != version_id:
                    raise RuntimeError("cohort version changed during activation")
            build["model_id"] = model_id
        else:
            _report(progress, 0.96, "Using audited monthly model")
        if build.get("qualified"):
            return build, "passed"
        if build.get("reason") == "cohort_requalification_failed":
            return None, "cohort_requalification_failed"
        if cohort["catalog_hash"] != catalog_hash:
            return None, "cohort_requalification_failed"
        prior = self.repository.latest_qualified(horizon)
        if prior is not None and prior.get("cohort_version_id") == version_id:
            activated = date.fromisoformat(prior["activated_session"])
            age = len(self.calendar.sessions(activated, completed)) - 1
            if 0 <= age <= MODEL_MAX_AGE_SESSIONS:
                return prior, "passed"
            if age > MODEL_MAX_AGE_SESSIONS:
                return None, "stale_model"
        if build.get("states"):
            # Return the rejected evidence to _refresh so the watched
            # contract's causal state gets its precise audit-gate reason.
            return build, "validation_failed"
        return None, build.get("reason", "model_not_ready")

    def _build_model(
        self,
        completed: date,
        horizon: int,
        cohort: dict,
        progress: Progress | None = None,
    ) -> dict:
        fit = []
        audit = []
        hashes = []
        selections = []
        revised_members = [dict(member) for member in cohort["members"]]
        revisions = []
        audit_tickers = _audit_tickers(cohort["members"])
        for index, member in enumerate(cohort["members"]):
            ticker = member["ticker"]
            try:
                bars = clean_completed(
                    self.prices.update(ticker, completed, full_refresh=True),
                    completed,
                    self.calendar,
                )
                selection, reason = self._selection(ticker, bars, completed, peer=True)
                if selection is None:
                    return self._failed_build(
                        completed,
                        horizon,
                        "cohort_requalification_failed"
                        if (
                            price_hash(bars.head(PREFIX_BARS)) != member["selection"]["prefix_hash"]
                            or cohort["catalog_hash"] != catalog()[1]
                        )
                        else reason or "peer_selection_failed",
                    )
                frozen = member["selection"]
                if (
                    selection.prefix_hash != frozen["prefix_hash"]
                    or selection.catalog_hash != frozen["catalog_hash"]
                    or selection.strategy.id != frozen["strategy"]["id"]
                ):
                    revised_members[index]["selection"] = _selection_payload(selection)
                    revised_members[index]["qualification_data_hash"] = price_hash(bars)
                    revisions.append(
                        {
                            "ticker": ticker,
                            "old_prefix_hash": frozen["prefix_hash"],
                            "new_prefix_hash": selection.prefix_hash,
                            "split_sessions_since_freeze": [
                                row["ts"].isoformat()
                                for row in bars.iter_rows(named=True)
                                if row["stock_splits"] > 0
                                and row["ts"]
                                > date.fromisoformat(cohort.get("created_session", "2025-12-31"))
                            ],
                        }
                    )
                states = strategy_states(bars, selection.strategy)
                if ticker in audit_tickers:
                    audit.extend(
                        observations(
                            ticker, bars, states, horizon, AUDIT_START, AUDIT_END, self.calendar
                        )
                    )
                else:
                    fit.extend(
                        observations(
                            ticker, bars, states, horizon, FIT_START, FIT_END, self.calendar
                        )
                    )
                hashes.append([ticker, price_hash(bars)])
                selections.append([ticker, selection.strategy.id, selection.prefix_hash])
                if index % 5 == 0:
                    _report(
                        progress,
                        0.65 + 0.3 * (index + 1) / len(cohort["members"]),
                        f"Auditing peer returns {index + 1}/{len(cohort['members'])}",
                    )
            except Exception:
                logger.exception("forecast model peer failed for %s", ticker)
                return self._failed_build(
                    completed,
                    horizon,
                    "cohort_requalification_failed" if revisions else "peer_data_missing",
                )
        evidence: dict[State, dict] = {}
        _report(progress, 0.95, "Auditing holdout skill")
        for state in ("long", "flat"):
            try:
                audited = fit_audit(fit, audit, horizon, state)
            except ValueError:
                logger.exception("forecast audit inputs invalid")
                return self._failed_build(completed, horizon, "validation_failed")
            evidence[state] = {
                "qualified": audited.qualified,
                "reason": audited.reason,
                "fit_values": list(audited.fit_values),
                "fit_peers": audited.fit_peers,
                "audit_peers": audited.audit_peers,
                "fit_samples": audited.fit_samples,
                "audit_samples": audited.audit_samples,
                "audit_blocks": audited.audit_blocks,
                "crps_skill_lower_90": audited.crps_skill_lower_90,
                "brier_delta": audited.brier_delta,
                "support_low": audited.support_low,
                "support_high": audited.support_high,
            }
        data_hash = _digest([hashes, selections])
        _, current_catalog_hash = catalog()
        model_id = _digest(
            [
                completed.isoformat(),
                horizon,
                cohort["candidate_hash"],
                current_catalog_hash,
                data_hash,
                evidence,
            ]
        )
        qualified = any(item["qualified"] for item in evidence.values())
        if revisions and not qualified:
            return self._failed_build(completed, horizon, "cohort_requalification_failed")
        version_id = cohort["version_id"]
        if revisions:
            revised_cohort = {
                **{key: value for key, value in cohort.items() if key != "version_id"},
                "catalog_hash": current_catalog_hash,
                "members": revised_members,
                "revised_session": completed.isoformat(),
                "revision_events": revisions,
            }
            version_id = _digest(revised_cohort)
            model_id = _digest([model_id, version_id])
        return {
            "model_id": model_id,
            "qualified": qualified,
            "reason": "passed" if qualified else "validation_failed",
            "activated_session": completed.isoformat(),
            "horizon": horizon,
            "data_hash": data_hash,
            "states": evidence,
            "candidate_hash": cohort["candidate_hash"],
            "catalog_hash": current_catalog_hash,
            "cohort_version_id": version_id,
            "cohort_revisions": revisions,
            **({"_pending_cohort": revised_cohort} if revisions else {}),
            "cohort_tickers": [m["ticker"] for m in cohort["members"]],
            "cohort_size": len(cohort["members"]),
            "member_data_hashes": hashes,
            "member_selections": selections,
        }

    @staticmethod
    def _failed_build(completed: date, horizon: int, reason: str) -> dict:
        return {
            "qualified": False,
            "reason": reason,
            "activated_session": completed.isoformat(),
            "horizon": horizon,
            "states": {},
            "data_hash": None,
            "attempted_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _unavailable(
        ticker: str,
        side: Side,
        strike: Decimal,
        expiry: date,
        completed: date,
        reason: str,
        *,
        selection: Selection | None = None,
        state: State | None = None,
        model: dict | None = None,
        evidence: dict | None = None,
    ) -> ForecastSnapshot:
        return ForecastSnapshot(
            ticker=ticker,
            side=side,
            strike=strike,
            expiry=expiry,
            as_of=completed,
            reason=reason,
            strategy_id=selection.strategy.id if selection else None,
            strategy_name=selection.strategy.name if selection else None,
            signal_state=state,
            model_id=model.get("model_id") if model else None,
            data_hash=model.get("data_hash") if model else None,
            cohort_size=model.get("cohort_size") if model else None,
            fit_peers=evidence.get("fit_peers") if evidence else None,
            audit_peers=evidence.get("audit_peers") if evidence else None,
            fit_samples=evidence.get("fit_samples") if evidence else None,
            audit_samples=evidence.get("audit_samples") if evidence else None,
            audit_blocks=evidence.get("audit_blocks") if evidence else None,
            crps_skill_lower_90=(evidence.get("crps_skill_lower_90") if evidence else None),
            brier_delta=evidence.get("brier_delta") if evidence else None,
        )
