"""Causal, independent expiry experiments using observed trade-bar marks.

No selection depends on an entry-session observation or a maturity label. Money
is calculated with Decimal; float columns are provided only for aggregation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from options_api.chain import _decision_metrics
from options_api.money import to_pct_tenths
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.research.historical_options.normalization import EASTERN

POLICIES = ("maximum_apr", "near_atm", "five_percent_below", "wider_comparator")
SCREENS = (
    "unscreened",
    "previous_transactions_5",
    "previous_volume_20",
    "previous_activity_5tx_20vol",
)
QUALITIES = ("primary", "reconciliation_inclusion")
HAIRCUTS = (Decimal("0"), Decimal("0.05"), Decimal("0.10"), Decimal("0.20"))
FEES = (Decimal("0"), Decimal("0.65"))
SLIPPAGE_BPS = (0, 10, 25)
CORE_START = date(2024, 10, 29)
CORE_END = date(2025, 12, 31)
SUPPLEMENT_START = date(2026, 1, 1)
RESEARCH_TICKERS = ("CIFR", "IREN", "NBIS", "WULF")


def decimal_price(value: Any) -> Decimal:
    """Preserve stored decimal tokens, including fractional cents."""
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite money input")
    return result


def displayed_apr(side: str, spot: Any, strike: Any, premium: Any, dte: int) -> int | None:
    """Use the app's time-value APR and native displayed rounding."""
    _, apr, _ = _decision_metrics(
        side, decimal_price(spot), decimal_price(strike), decimal_price(premium), dte
    )
    return None if apr is None else to_pct_tenths(apr)


def _rank_contracts(
    candidates: list[dict[str, Any]],
    spot: Any,
    side: str,
    policy: str,
    origin: date,
    *,
    screen: str = "unscreened",
    haircut: Decimal = Decimal("0"),
) -> list[dict[str, Any]]:
    """Rank exclusively from exact-session decision features."""
    current = decimal_price(spot)
    if current <= 0 or side not in {"call", "put"} or policy not in POLICIES:
        return []
    if screen not in SCREENS:
        raise ValueError("unknown liquidity screen")
    ranked = []
    for row in candidates:
        if not row.get("valid", True):
            continue
        if not _positive(row.get("strike")) or not _positive(row.get("mark")):
            continue
        strike, premium = decimal_price(row["strike"]), decimal_price(row["mark"])
        volume, transactions = row["volume"], row["transactions"]
        if strike <= 0 or premium <= 0 or volume is None or volume <= 0:
            continue
        if screen in {"previous_transactions_5", "previous_activity_5tx_20vol"} and (
            transactions is None or transactions < 5
        ):
            continue
        if screen in {"previous_volume_20", "previous_activity_5tx_20vol"} and volume < 20:
            continue
        ratio = strike / current
        if policy == "maximum_apr":
            eligible, distance = Decimal("0.90") <= ratio < 1, Decimal(0)
        elif policy == "near_atm":
            eligible, distance = Decimal("0.975") <= ratio < 1, 1 - ratio
        elif policy == "five_percent_below":
            eligible = Decimal("0.925") <= ratio <= Decimal("0.975")
            distance = abs(ratio - Decimal("0.95"))
        else:
            target, lower, upper = (
                ("1.05", "1.025", "1.075") if side == "call" else ("0.90", "0.875", "0.925")
            )
            eligible = Decimal(lower) <= ratio <= Decimal(upper)
            distance = abs(ratio - Decimal(target))
        if not eligible:
            continue
        apr = displayed_apr(
            side, current, strike, premium * (1 - haircut), (row["expiry"] - origin).days
        )
        if apr is None:
            continue
        key = (-apr, -volume, strike, row["contract"])
        if policy != "maximum_apr":
            key = (distance, *key)
        ranked.append((key, row))
    return [row for _, row in sorted(ranked, key=lambda item: item[0])]


def select_contract(
    candidates: list[dict[str, Any]],
    spot: Any,
    side: str,
    policy: str,
    origin: date,
    *,
    screen: str = "unscreened",
    haircut: Decimal = Decimal("0"),
) -> dict[str, Any] | None:
    ranked = _rank_contracts(candidates, spot, side, policy, origin, screen=screen, haircut=haircut)
    return ranked[0] if ranked else None


def _rank_stability(baseline: list[dict], changed: list[dict]) -> dict[str, Any]:
    baseline_ids = [row["contract"] for row in baseline]
    changed_ids = [row["contract"] for row in changed]
    matched = set(baseline_ids) & set(changed_ids)
    baseline_matched = [contract for contract in baseline_ids if contract in matched]
    changed_ranks = {
        contract: rank
        for rank, contract in enumerate(contract for contract in changed_ids if contract in matched)
    }
    count = len(matched)
    differences = sum(
        (rank - changed_ranks[contract]) ** 2 for rank, contract in enumerate(baseline_matched)
    )
    top_count = min(3, len(baseline_ids), len(changed_ids))
    return {
        "candidate_count": len(baseline_ids),
        "haircut_candidate_count": len(changed_ids),
        "matched_rank_count": count,
        "spearman_rank_correlation": 1 - 6 * differences / (count * (count**2 - 1))
        if count >= 2
        else None,
        "top_three_overlap": len(set(baseline_ids[:3]) & set(changed_ids[:3])) / top_count
        if top_count
        else None,
    }


def expiry_accounting(
    side: str,
    strike: Any,
    stock_entry: Any,
    premium: Any,
    expiry_close: Any,
    *,
    haircut: Any = 0,
    fee: Any = 0,
    stock_slippage_bps: int = 0,
) -> dict[str, Decimal]:
    """Exact independent-trade accounting; gross capital and benchmark agree."""
    if side not in {"call", "put"}:
        raise ValueError("unknown option side")
    strike, stock_entry, premium, expiry_close = map(
        decimal_price, (strike, stock_entry, premium, expiry_close)
    )
    haircut, fee = decimal_price(haircut), decimal_price(fee)
    if not 0 <= haircut <= 1 or fee < 0 or stock_slippage_bps < 0:
        raise ValueError("invalid cost assumption")
    if strike <= 0 or stock_entry <= 0 or premium <= 0 or expiry_close <= 0:
        raise ValueError("nonpositive observed price")
    shares = Decimal(100)
    effective_entry = stock_entry * (1 + Decimal(stock_slippage_bps) / 10000)
    stressed_premium = premium * (1 - haircut)
    if side == "call":
        capital = shares * effective_entry
        pnl = shares * (min(expiry_close, strike) - effective_entry + stressed_premium) - fee
        benchmark = shares * (expiry_close - effective_entry)
        upside_forgone = shares * max(expiry_close - strike, Decimal(0))
    else:
        capital = shares * strike
        pnl = shares * (stressed_premium - max(strike - expiry_close, Decimal(0))) - fee
        benchmark = Decimal(0)
        upside_forgone = Decimal(0)
    net_outlay = capital - shares * stressed_premium + fee
    return {
        "pnl": pnl,
        "capital": capital,
        "net_outlay": net_outlay,
        "net_premium": shares * stressed_premium - fee,
        "stock_entry": effective_entry,
        "premium": stressed_premium,
        "benchmark_pnl": benchmark,
        "return": pnl / capital,
        "benchmark_return": benchmark / capital,
        "excess_return": (pnl - benchmark) / capital,
        "upside_forgone": upside_forgone,
    }


class _TableWriter:
    """Bounded rows, fixed schema, including empty output tables."""

    def __init__(self, path: Path, schema: pa.Schema) -> None:
        self.path, self.schema = path, schema
        self.writer = pq.ParquetWriter(path, schema, compression="zstd")
        self.rows: list[dict[str, Any]] = []
        self.count = 0

    def append(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        self.count += 1
        if len(self.rows) >= 4096:
            self.flush()

    def flush(self) -> None:
        if self.rows:
            self.writer.write_table(pa.Table.from_pylist(self.rows, schema=self.schema))
            self.rows.clear()

    def close(self) -> None:
        self.flush()
        self.writer.close()


def _schema(fields: list[tuple[str, pa.DataType]]) -> pa.Schema:
    return pa.schema(fields)


_KEY_FIELDS = [
    (name, pa.string())
    for name in (
        "selection_id",
        "ticker",
        "panel",
        "side",
        "policy",
        "screen",
        "quality",
        "contract",
    )
] + [(name, pa.date32()) for name in ("origin", "entry_session", "expiry", "expiry_session")]
_KEY_FIELDS += [
    ("horizon", pa.int32()),
    ("primary_eligible", pa.bool_()),
    ("status", pa.string()),
    ("reason", pa.string()),
]
SELECTION_SCHEMA = _schema(
    _KEY_FIELDS
    + [
        ("decision_stock_close", pa.float64()),
        ("strike", pa.float64()),
        ("decision_mark", pa.float64()),
        ("decision_volume", pa.float64()),
        ("decision_transactions", pa.float64()),
        ("displayed_apr_pct_tenths", pa.int64()),
        ("calendar_dte", pa.int32()),
        ("decision_reconciliation_failed", pa.bool_()),
        ("entry_reconciliation_failed", pa.bool_()),
        ("stock_entry", pa.float64()),
        ("option_entry", pa.float64()),
        ("expiry_close", pa.float64()),
        ("opening_start", pa.timestamp("us", tz="UTC")),
        ("opening_end", pa.timestamp("us", tz="UTC")),
        ("entry_time_provenance", pa.string()),
    ]
)
OUTCOME_SCHEMA = _schema(
    _KEY_FIELDS
    + [
        ("haircut", pa.float64()),
        ("fee", pa.float64()),
        ("stock_slippage_bps", pa.int32()),
        *[
            (name, pa.float64())
            for name in (
                "pnl",
                "capital",
                "net_outlay",
                "net_premium",
                "stock_entry",
                "premium",
                "benchmark_pnl",
                "return",
                "benchmark_return",
                "excess_return",
                "upside_forgone",
            )
        ],
        *[
            (f"{name}_decimal", pa.string())
            for name in ("pnl", "capital", "net_outlay", "net_premium", "benchmark_pnl")
        ],
        ("itm", pa.bool_()),
        ("atm", pa.bool_()),
        ("loss", pa.bool_()),
    ]
)
TIMING_SCHEMA = _schema(
    _KEY_FIELDS
    + [
        ("mark_hour_et", pa.int32()),
        ("mark", pa.float64()),
        ("stock_entry", pa.float64()),
        ("mark_start", pa.timestamp("us", tz="UTC")),
        ("mark_end", pa.timestamp("us", tz="UTC")),
        ("pnl", pa.float64()),
        ("return", pa.float64()),
        ("benchmark_return", pa.float64()),
        ("interpretation", pa.string()),
    ]
)
RANK_SCHEMA = _schema(
    _KEY_FIELDS
    + [
        ("haircut", pa.float64()),
        ("haircut_contract", pa.string()),
        ("unchanged", pa.bool_()),
        ("candidate_count", pa.int32()),
        ("haircut_candidate_count", pa.int32()),
        ("matched_rank_count", pa.int32()),
        ("spearman_rank_correlation", pa.float64()),
        ("top_three_overlap", pa.float64()),
        ("baseline_apr_pct_tenths", pa.int64()),
        ("haircut_apr_pct_tenths", pa.int64()),
    ]
)


def _positive(value: Any) -> bool:
    try:
        return value is not None and decimal_price(value) > 0
    except (ValueError, ArithmeticError):
        return False


def _finite(value: Any) -> bool:
    try:
        return value is not None and decimal_price(value).is_finite()
    except (ValueError, ArithmeticError):
        return False


def _panel(ticker: str, origin: date, maturity: date, supplement_end: date | None) -> str:
    if CORE_START <= origin <= CORE_END and maturity <= CORE_END:
        return "primary"
    if (
        ticker != "NBIS"
        and supplement_end
        and SUPPLEMENT_START <= origin <= supplement_end
        and maturity <= supplement_end
    ):
        return "supplement"
    return "other_eligible"


def _action_reason(
    ticker: str,
    origin: date,
    entry: date,
    maturity: date,
    actions: dict[str, list[tuple[date, float, float]]],
    unknown_actions: dict[str, set[date]],
    stock_sessions: dict[str, set[date]],
    interval_sessions: tuple[date, ...],
) -> str | None:
    if any(
        session not in stock_sessions[ticker] or session in unknown_actions[ticker]
        for session in interval_sessions
    ):
        return "unknown_corporate_action_metadata"
    if any(session > origin for session in unknown_actions[ticker]):
        return "unverifiable_frozen_stock_basis"
    for session, dividend, split in actions[ticker]:
        if split and session > origin:
            # A later split also changes the frozen, split-normalized historical basis.
            return "unverifiable_split_price_basis"
        if dividend and entry <= session <= maturity:
            return "corporate_action_interval"
    return None


def run_strategies(
    days: pl.DataFrame,
    stocks: pl.DataFrame,
    hours: pl.DataFrame,
    output: Path,
) -> dict[str, Any]:
    """Write causal selections, statuses, fixed-cost outcomes and diagnostics."""
    output.mkdir(parents=True, exist_ok=True)
    calendar = SessionCalendar()
    stock_lookup = {(row["ticker"], row["ts"]): row for row in stocks.iter_rows(named=True)}
    actions: dict[str, list[tuple[date, float, float]]] = defaultdict(list)
    unknown_actions: dict[str, set[date]] = defaultdict(set)
    stock_sessions: dict[str, set[date]] = defaultdict(set)
    latest: dict[str, date] = {}
    for (ticker, session), row in stock_lookup.items():
        stock_sessions[ticker].add(session)
        latest[ticker] = max(session, latest.get(ticker, session))
        dividend, split = row.get("dividends"), row.get("stock_splits")
        if not _finite(dividend) or not _finite(split):
            unknown_actions[ticker].add(session)
        elif dividend or split:
            actions[ticker].append((session, dividend, split))
    supplement_end = min((latest[t] for t in ("CIFR", "IREN", "WULF") if t in latest), default=None)
    if supplement_end is not None:
        supplement_end = min(supplement_end, date(2026, 9, 28))
    ordinals = (
        {
            session: number
            for number, session in enumerate(
                calendar.sessions(
                    min(days["session"].min(), stocks["ts"].min()), days["expiry_session"].max()
                )
            )
        }
        if not days.is_empty() and not stocks.is_empty()
        else {}
    )
    writers = {
        "selections": _TableWriter(output / "selections.parquet", SELECTION_SCHEMA),
        "outcomes": _TableWriter(output / "outcomes.parquet", OUTCOME_SCHEMA),
        "timing": _TableWriter(output / "premium_timing.parquet", TIMING_SCHEMA),
        "rank": _TableWriter(output / "ranking_stability.parquet", RANK_SCHEMA),
        "exclusions": _TableWriter(output / "strategy_exclusions.parquet", SELECTION_SCHEMA),
    }
    counts: Counter[str] = Counter()
    try:
        for ticker in sorted(days["ticker"].unique().to_list()):
            ticker_days = days.filter(pl.col("ticker") == ticker).sort(
                ["session", "side", "expiry_session", "strike", "contract"]
            )
            day_lookup = {
                (row["contract"], row["session"]): row for row in ticker_days.iter_rows(named=True)
            }
            timing_lookup = {}
            for row in (
                hours.filter(
                    (pl.col("ticker") == ticker)
                    & pl.col("regular_session")
                    & pl.col("valid")
                    & pl.col("hour").is_in([9, 10, 13, 15])
                )
                .select("contract", "session", "hour", "open", "start", "end")
                .iter_rows()
            ):
                contract, session, hour, mark, start, end = row
                timing_lookup[contract, session, hour] = (mark, start, end)
            for (origin,), origin_frame in ticker_days.partition_by(
                "session", as_dict=True, maintain_order=True
            ).items():
                decision_stock = stock_lookup.get((ticker, origin))
                if not decision_stock or not _positive(decision_stock["close"]):
                    counts["decision_missing_verified_stock"] += origin_frame.height
                    writers["exclusions"].append(
                        {
                            "selection_id": f"{ticker}|{origin}|unverified_stock",
                            "ticker": ticker,
                            "origin": origin,
                            "panel": "coverage_only",
                            "status": "excluded",
                            "reason": "decision_missing_verified_stock",
                            "primary_eligible": False,
                        }
                    )
                    continue
                groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
                for row in origin_frame.iter_rows(named=True):
                    horizon = ordinals.get(row["expiry_session"], -100) - ordinals.get(origin, 0)
                    if 1 <= horizon <= 25:
                        groups[row["side"], horizon].append(row)
                    else:
                        counts["outside_1_25_horizons"] += 1
                entry_session = calendar.offset(origin, 1)
                for (side, horizon), candidates in sorted(groups.items()):
                    panel = _panel(ticker, origin, candidates[0]["expiry_session"], supplement_end)
                    for screen in SCREENS:
                        for policy in POLICIES:
                            counts["scheduled_selection_groups"] += 1
                            selected = select_contract(
                                candidates,
                                decision_stock["close"],
                                side,
                                policy,
                                origin,
                                screen=screen,
                            )
                            if selected is None:
                                counts["no_policy_contract"] += 1
                                writers["exclusions"].append(
                                    {
                                        "selection_id": (
                                            f"{ticker}|{origin}|{side}|{horizon}|{policy}|{screen}"
                                        ),
                                        "ticker": ticker,
                                        "origin": origin,
                                        "entry_session": entry_session,
                                        "panel": panel,
                                        "side": side,
                                        "horizon": horizon,
                                        "policy": policy,
                                        "screen": screen,
                                        "status": "no_selection",
                                        "reason": "no_eligible_policy_contract",
                                        "primary_eligible": False,
                                    }
                                )
                                continue
                            selection_id = f"{ticker}|{origin}|{side}|{horizon}|{policy}|{screen}"
                            entry_day = day_lookup.get((selected["contract"], entry_session))
                            entry_stock = stock_lookup.get((ticker, entry_session))
                            expiry_stock = stock_lookup.get((ticker, selected["expiry_session"]))
                            decision_failed = bool(selected.get("reconciliation_failed", False))
                            entry_failed = bool(
                                entry_day and entry_day.get("reconciliation_failed")
                            )
                            maturity = selected["expiry_session"]
                            status, reason = "scored", None
                            if not entry_stock or not _positive(entry_stock["open"]):
                                status, reason = "no_entry", "missing_verified_stock_open"
                            elif not entry_day or not _positive(entry_day.get("opening_open")):
                                status, reason = "no_entry", "missing_opening_bucket"
                            elif not expiry_stock or not _positive(expiry_stock["close"]):
                                status, reason = "excluded", "missing_verified_expiry_close"
                            elif not entry_day.get("valid", True):
                                status, reason = "no_entry", "invalid_entry_day"
                            else:
                                action_reason = _action_reason(
                                    ticker,
                                    origin,
                                    entry_session,
                                    maturity,
                                    actions,
                                    unknown_actions,
                                    stock_sessions,
                                    calendar.sessions(origin, maturity),
                                )
                                if action_reason:
                                    status, reason = "excluded", action_reason
                            base = {
                                "selection_id": selection_id,
                                "ticker": ticker,
                                "panel": panel,
                                "origin": origin,
                                "entry_session": entry_session,
                                "expiry": selected["expiry"],
                                "expiry_session": maturity,
                                "horizon": horizon,
                                "side": side,
                                "policy": policy,
                                "screen": screen,
                                "contract": selected["contract"],
                                "decision_stock_close": float(decision_stock["close"]),
                                "strike": float(selected["strike"]),
                                "decision_mark": float(selected["mark"]),
                                "decision_volume": float(selected["volume"]),
                                "decision_transactions": float(selected["transactions"] or 0),
                                "displayed_apr_pct_tenths": displayed_apr(
                                    side,
                                    decision_stock["close"],
                                    selected["strike"],
                                    selected["mark"],
                                    (selected["expiry"] - origin).days,
                                ),
                                "calendar_dte": (selected["expiry"] - origin).days,
                                "decision_reconciliation_failed": decision_failed,
                                "entry_reconciliation_failed": entry_failed,
                                "stock_entry": entry_stock["open"] if entry_stock else None,
                                "option_entry": entry_day["opening_open"] if entry_day else None,
                                "expiry_close": expiry_stock["close"] if expiry_stock else None,
                                "opening_start": entry_day["opening_start"] if entry_day else None,
                                "opening_end": entry_day["opening_end"] if entry_day else None,
                                "entry_time_provenance": "vendor_hour_first_trade_time_unknown",
                            }
                            # Ranking is a decision-only diagnostic, including excluded outcomes.
                            if policy == "maximum_apr":
                                baseline_ranking = _rank_contracts(
                                    candidates,
                                    decision_stock["close"],
                                    side,
                                    policy,
                                    origin,
                                    screen=screen,
                                )
                                for haircut in HAIRCUTS:
                                    alternative_ranking = _rank_contracts(
                                        candidates,
                                        decision_stock["close"],
                                        side,
                                        policy,
                                        origin,
                                        screen=screen,
                                        haircut=haircut,
                                    )
                                    alternative = (
                                        alternative_ranking[0] if alternative_ranking else None
                                    )
                                    writers["rank"].append(
                                        {
                                            **base,
                                            "haircut": float(haircut),
                                            "haircut_contract": alternative["contract"]
                                            if alternative
                                            else None,
                                            "unchanged": bool(
                                                alternative
                                                and alternative["contract"] == selected["contract"]
                                            ),
                                            **_rank_stability(
                                                baseline_ranking, alternative_ranking
                                            ),
                                            "baseline_apr_pct_tenths": base[
                                                "displayed_apr_pct_tenths"
                                            ],
                                            "haircut_apr_pct_tenths": displayed_apr(
                                                side,
                                                decision_stock["close"],
                                                alternative["strike"],
                                                decimal_price(alternative["mark"]) * (1 - haircut),
                                                (alternative["expiry"] - origin).days,
                                            )
                                            if alternative
                                            else None,
                                        }
                                    )
                            for quality in QUALITIES:
                                quality_status, quality_reason = status, reason
                                if (
                                    quality == "primary"
                                    and status == "scored"
                                    and (decision_failed or entry_failed)
                                ):
                                    quality_status, quality_reason = (
                                        "excluded",
                                        "reconciliation_failure",
                                    )
                                row = {
                                    **base,
                                    "quality": quality,
                                    "status": quality_status,
                                    "reason": quality_reason,
                                    "primary_eligible": status == "scored"
                                    and not decision_failed
                                    and not entry_failed,
                                }
                                writers["selections"].append(row)
                                counts[f"{quality}_{quality_status}"] += 1
                                if quality_status != "scored":
                                    writers["exclusions"].append(row)
                                # One outcome row per cost assumption, including missing/excluded
                                # labels retain failed entries in the denominators.
                                slips = SLIPPAGE_BPS if side == "call" else (0,)
                                for haircut in HAIRCUTS:
                                    for fee in FEES:
                                        for slip in slips:
                                            outcome = {
                                                **row,
                                                "haircut": float(haircut),
                                                "fee": float(fee),
                                                "stock_slippage_bps": slip,
                                            }
                                            if quality_status == "scored":
                                                money = expiry_accounting(
                                                    side,
                                                    selected["strike"],
                                                    entry_stock["open"],
                                                    entry_day["opening_open"],
                                                    expiry_stock["close"],
                                                    haircut=haircut,
                                                    fee=fee,
                                                    stock_slippage_bps=slip,
                                                )
                                                outcome.update(
                                                    {
                                                        key: float(value)
                                                        for key, value in money.items()
                                                    }
                                                )
                                                outcome.update(
                                                    {
                                                        f"{key}_decimal": str(money[key])
                                                        for key in (
                                                            "pnl",
                                                            "capital",
                                                            "net_outlay",
                                                            "net_premium",
                                                            "benchmark_pnl",
                                                        )
                                                    }
                                                )
                                                close, strike = map(
                                                    decimal_price,
                                                    (expiry_stock["close"], selected["strike"]),
                                                )
                                                outcome.update(
                                                    itm=close > strike
                                                    if side == "call"
                                                    else close < strike,
                                                    atm=close == strike,
                                                    loss=money["pnl"] < 0,
                                                )
                                            writers["outcomes"].append(outcome)
                                for hour in (9, 10, 13, 15):
                                    mark = timing_lookup.get(
                                        (selected["contract"], entry_session, hour)
                                    )
                                    diagnostic = {
                                        **row,
                                        "mark_hour_et": hour,
                                        "interpretation": (
                                            "premium_mark_sensitivity_fixed_stock_open"
                                        ),
                                    }
                                    bucket_start = datetime.combine(
                                        entry_session, time(hour), tzinfo=EASTERN
                                    )
                                    bucket_end = bucket_start + timedelta(hours=1)
                                    scheduled_open = calendar.exchange.session_open(
                                        entry_session
                                    ).to_pydatetime()
                                    scheduled_close = calendar.exchange.session_close(
                                        entry_session
                                    ).to_pydatetime()
                                    if (bucket_start >= scheduled_close
                                            or bucket_end <= scheduled_open):
                                        diagnostic.update(
                                            status="outside_session",
                                            reason="bucket_outside_scheduled_session",
                                        )
                                        writers["timing"].append(diagnostic)
                                        continue
                                    if mark and _positive(mark[0]):
                                        diagnostic.update(
                                            mark=mark[0], mark_start=mark[1], mark_end=mark[2]
                                        )
                                    if quality_status == "scored" and mark and _positive(mark[0]):
                                        money = expiry_accounting(
                                            side,
                                            selected["strike"],
                                            entry_stock["open"],
                                            mark[0],
                                            expiry_stock["close"],
                                        )
                                        diagnostic.update(
                                            {
                                                key: float(money[key])
                                                for key in ("pnl", "return", "benchmark_return")
                                            }
                                        )
                                    elif quality_status == "scored":
                                        diagnostic.update(
                                            status="missing_mark", reason="missing_session_bucket"
                                        )
                                    writers["timing"].append(diagnostic)
            day_lookup.clear()
            timing_lookup.clear()
    finally:
        for writer in writers.values():
            writer.close()
    aggregate = pl.scan_parquet(output / "outcomes.parquet").filter(pl.col("status") == "scored")
    group_columns = [
        "panel",
        "quality",
        "screen",
        "policy",
        "side",
        "haircut",
        "fee",
        "stock_slippage_bps",
    ]
    summary = (
        aggregate.group_by(group_columns)
        .agg(
            pl.len().alias("scored_experiments"),
            pl.col("return").mean().alias("mean_return"),
            pl.col("benchmark_return").mean().alias("mean_benchmark_return"),
            pl.col("excess_return").mean().alias("mean_excess_return"),
            pl.col("return").quantile(0.05, interpolation="lower").alias("fifth_percentile_return"),
            pl.col("loss").mean().alias("loss_frequency"),
            pl.col("itm").mean().alias("expiry_close_itm"),
            pl.col("atm").mean().alias("expiry_close_atm"),
            pl.col("upside_forgone").mean().alias("mean_upside_forgone"),
        )
        .sort(group_columns)
        .collect(engine="streaming")
    )
    summary.write_parquet(output / "strategy_summary.parquet", compression="zstd")
    ranks = pl.scan_parquet(output / "ranking_stability.parquet")
    ranking = (
        ranks.group_by("panel", "side", "screen", "haircut")
        .agg(
            pl.len().alias("ranked_groups"),
            pl.col("unchanged").mean().alias("identity_stability"),
            pl.col("spearman_rank_correlation").mean().alias("mean_spearman_rank_correlation"),
            pl.col("top_three_overlap").mean().alias("mean_top_three_overlap"),
        )
        .sort("panel", "side", "screen", "haircut")
    )
    ranking = ranking.collect(engine="streaming")
    ranking.write_parquet(output / "ranking_summary.parquet", compression="zstd")
    timing = pl.scan_parquet(output / "premium_timing.parquet")
    timing_summary = (
        timing.group_by("panel", "quality", "screen", "policy", "side", "mark_hour_et", "status")
        .agg(pl.len().alias("experiments"), pl.col("return").mean().alias("mean_return"))
        .sort("panel", "quality", "screen", "policy", "side", "mark_hour_et", "status")
    )
    timing_summary = timing_summary.collect(engine="streaming")
    timing_summary.write_parquet(output / "timing_summary.parquet", compression="zstd")
    exclusion_summary = (
        pl.scan_parquet(output / "strategy_exclusions.parquet")
        .group_by("panel", "quality", "reason")
        .agg(pl.len().alias("excluded_records"))
        .sort("panel", "quality", "reason")
        .collect()
    )
    exclusion_summary.write_parquet(
        output / "strategy_exclusion_summary.parquet", compression="zstd"
    )
    return {
        "counts": dict(sorted(counts.items())),
        "rows": {name: writer.count for name, writer in writers.items()},
        "supplement_cutoff": str(supplement_end),
        "cost_grid": summary.to_dicts(),
        "ranking_stability": ranking.to_dicts(),
        "timing_sensitivity": timing_summary.to_dicts(),
        "exclusions": exclusion_summary.to_dicts(),
        "interpretation": "Independent expiry experiments, conditional on action-safe intervals; "
        "trade-bar price proxies with unknown first-trade times, "
        "not executable quotes or assignment.",
    }
