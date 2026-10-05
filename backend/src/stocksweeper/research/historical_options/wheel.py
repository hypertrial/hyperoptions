"""Next-leg wheel research from a verified, current-vintage frozen archive.

Positions start at current wealth, not historical cost basis. Terminal exercise
is a scenario inferred from expiry Close, never a record of actual assignment.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import numpy as np
import polars as pl
import pyarrow as pa

from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.research.historical_options.artifacts import (
    atomic_directory,
    finalize_manifest,
    safe_path,
)
from stocksweeper.research.historical_options.runner import environment_versions, peak_rss_bytes
from stocksweeper.research.historical_options.snapshot import environment_metadata, verify_snapshot
from stocksweeper.research.historical_options.strategies import (
    CORE_END,
    CORE_START,
    RESEARCH_TICKERS,
    _action_reason,
    _finite,
    _panel,
    _positive,
    _TableWriter,
    decimal_price,
    expiry_accounting,
)
from stocksweeper.research.historical_options.wheel_statistics import (
    moving_block_interval,
    pareto_representatives,
    summarize,
)

HOLDOUT = date(2025, 9, 17)
SUPPLEMENT_END = date(2026, 9, 28)
DEPTHS = ("0-2.5", "2.5-5", "5-10", "10-20", "20+")
HORIZONS = ("1", "2-5", "6-10", "11-25")
CONDITIONS = (
    "volatility_low",
    "volatility_middle",
    "volatility_high",
    "trend_positive",
    "trend_nonpositive",
    "activity_5tx_20vol",
)
SETTINGS = {
    "version": 1,
    "research_vintage": "current-vintage retrospective",
    "reference_haircut": 0.10,
    "option_fee": 0.65,
    "premium_haircuts": [0, 0.05, 0.10, 0.20],
    "buy_write_slippage_bps": [0, 10, 25],
    "cash_yield": 0,
    "depth_bands": list(DEPTHS),
    "horizon_bands": list(HORIZONS),
    "holdout_start": HOLDOUT.isoformat(),
    "supplement_end": SUPPLEMENT_END.isoformat(),
    "capital_days": "inclusive calendar dates from entry through expiry",
    "block_length": 26,
    "bootstrap_draws": 2000,
    "bootstrap_seed": 1729,
    "minimum_inference_dates": 20,
    "minimum_inference_blocks": 8,
    "minimum_checklist_dates": 20,
    "minimum_checklist_expiries": 4,
    "maximum_conditions": 1,
    "conditions": list(CONDITIONS),
    "assignment": "strict expiry-close terminal settlement proxy; ATM ambiguous",
}


def assessment_days(entry: date, expiry: date) -> int:
    if expiry < entry:
        raise ValueError("expiry cannot precede entry")
    return (expiry - entry).days + 1


def depth_band(spot: object, strike: object) -> str | None:
    if not _positive(spot) or not _positive(strike):
        return None
    depth = 1 - decimal_price(strike) / decimal_price(spot)
    if depth <= 0:
        return None
    for upper, label in zip((".025", ".05", ".10", ".20"), DEPTHS, strict=False):
        if depth < Decimal(upper):
            return label
    return "20+"


def horizon_band(horizon: int) -> str | None:
    for low, high, label in ((1, 1, "1"), (2, 5, "2-5"), (6, 10, "6-10"), (11, 25, "11-25")):
        if low <= horizon <= high:
            return label
    return None


def stock_features(stocks: pl.DataFrame) -> dict[tuple[str, date], dict]:
    """Require contiguous completed exchange sessions; never backfill history."""
    result = {}
    if stocks.is_empty():
        return result
    calendar = SessionCalendar()
    dates = calendar.sessions(stocks["ts"].min(), stocks["ts"].max())
    ordinal = {session: i for i, session in enumerate(dates)}
    for frame in stocks.sort(["ticker", "ts"]).partition_by("ticker", maintain_order=True):
        history: list[tuple[date, float]] = []
        for row in frame.iter_rows(named=True):
            session, close = row["ts"], row["close"]
            if (
                not _positive(close)
                or session not in ordinal
                or (history and ordinal[session] != ordinal[history[-1][0]] + 1)
            ):
                history = []
            if _positive(close) and session in ordinal:
                history.append((session, float(close)))
            features = {"volatility20": None, "return5": None, "return20": None}
            for lag in (5, 20):
                if len(history) > lag:
                    features[f"return{lag}"] = close / history[-lag - 1][1] - 1
            if len(history) >= 21:
                log_returns = np.diff(np.log([value for _, value in history[-21:]]))
                features["volatility20"] = float(np.std(log_returns, ddof=1) * np.sqrt(252))
            result[(row["ticker"], session)] = features
    return result


def select_wheel_contract(
    candidates: list[dict],
    spot: object,
    entry_session: date,
    haircut: object = 0.10,
    fee: object = 0.65,
) -> dict | None:
    """Rank decision-only capped gain/day, including negative valid gains."""
    if not _positive(spot):
        return None
    spot, haircut, fee = map(decimal_price, (spot, haircut, fee))
    if not 0 <= haircut <= 1 or fee < 0:
        raise ValueError("invalid cost assumption")
    ranked = []
    for row in candidates:
        if (
            not row.get("valid", True)
            or row["side"] not in {"call", "put"}
            or depth_band(spot, row["strike"]) is None
            or not _positive(row["mark"])
            or not _positive(row.get("volume"))
        ):
            continue
        strike, mark = map(decimal_price, (row["strike"], row["mark"]))
        capital = spot if row["side"] == "call" else strike
        intrinsic = spot - strike if row["side"] == "call" else Decimal(0)
        gain = mark * (1 - haircut) - intrinsic - fee / 100
        reward = gain / capital / assessment_days(entry_session, row["expiry_session"])
        ranked.append(
            (
                (-reward, -(row.get("transactions") or 0), -row["volume"], strike, row["contract"]),
                row,
            )
        )
    return min(ranked, key=lambda item: item[0])[1] if ranked else None


def _period(panel: str, origin: date, expiry: date) -> str:
    if panel == "primary":
        if expiry < HOLDOUT:
            return "development"
        return "holdout" if origin >= HOLDOUT else "purged"
    return "supplement" if panel == "supplement" else "other_eligible"


_STRINGS = (
    "opportunity_id",
    "ticker",
    "contract",
    "side",
    "panel",
    "period",
    "depth_band",
    "horizon_band",
    "rule_id",
    "status",
    "reason",
    "ending_state",
    "pnl_decimal",
    "capital_decimal",
    "net_outlay_decimal",
    "net_premium_decimal",
    "entry_time_provenance",
    "outcome_unavailable_reason",
)
_DATES = ("origin", "entry_session", "expiry", "expiry_session")
_FLOATS = (
    "strike",
    "decision_stock_close",
    "decision_mark",
    "decision_depth",
    "decision_intrinsic",
    "decision_time_value",
    "decision_premium_yield",
    "decision_time_value_yield",
    "decision_breakeven_cushion",
    "decision_capped_return",
    "decision_volume",
    "decision_transactions",
    "volatility20",
    "return5",
    "return20",
    "stock_entry",
    "option_entry",
    "expiry_close",
    "haircut",
    "fee",
    "return",
    "baseline_return",
    "pnl",
    "capital",
    "net_outlay",
    "net_premium",
    "entry_breakeven_cushion",
    "entry_time_value_yield",
    "entry_capped_return",
    "ending_cash",
    "ending_share_value",
    "underlying_min_return",
    "upside_forgone",
    "buy_write_return_10bp",
    "buy_write_return_25bp",
)
DETAIL_SCHEMA = pa.schema(
    [(name, pa.string()) for name in _STRINGS]
    + [(name, pa.date32()) for name in _DATES]
    + [(name, pa.float64()) for name in _FLOATS]
    + [
        (name, pa.int32())
        for name in (
            "horizon",
            "calendar_dte",
            "assessment_days",
            "regular_hours",
            "ending_shares",
            "stock_slippage_bps",
        )
    ]
    + [
        (name, pa.bool_())
        for name in ("traded", "expiry_itm", "expiry_atm", "underlying_breakeven_breach")
    ]
    + [(name, pa.timestamp("us", tz="UTC")) for name in ("opening_start", "opening_end")]
)


def _decision_features(row: dict, spot: object, entry: date) -> dict:
    spot, strike, mark = map(decimal_price, (spot, row["strike"], row["mark"]))
    intrinsic = max(spot - strike, Decimal(0)) if row["side"] == "call" else Decimal(0)
    capital = spot if row["side"] == "call" else strike
    net_premium = mark * Decimal(".9") - Decimal(".0065")
    breakeven = spot - net_premium if row["side"] == "call" else strike - net_premium
    return {
        "decision_stock_close": float(spot),
        "decision_mark": float(mark),
        "decision_depth": float(1 - strike / spot),
        "decision_intrinsic": float(intrinsic),
        "decision_time_value": float(mark - intrinsic),
        "decision_premium_yield": float(mark / capital),
        "decision_time_value_yield": float((mark - intrinsic) / capital),
        "decision_breakeven_cushion": float(1 - breakeven / spot),
        "decision_capped_return": float((net_premium - intrinsic) / capital),
        "decision_volume": float(row["volume"]),
        "decision_transactions": float(row.get("transactions") or 0),
        "calendar_dte": (row["expiry"] - row["session"]).days,
        "assessment_days": assessment_days(entry, row["expiry_session"]),
        "regular_hours": row.get("regular_hours"),
    }


def _economics(row: dict, stock_path: list[dict], haircut: float = 0.10) -> dict:
    money = expiry_accounting(
        row["side"],
        row["strike"],
        row["stock_entry"],
        row["option_entry"],
        row["expiry_close"],
        haircut=haircut,
        fee=0.65,
    )
    strike, start, close = map(
        decimal_price, (row["strike"], row["stock_entry"], row["expiry_close"])
    )
    is_call = row["side"] == "call"
    itm = close > strike if is_call else close < strike
    shares = 0 if (itm if is_call else not itm) else 100
    if is_call:
        cash = money["net_premium"] + (100 * strike if itm else 0)
    else:
        cash = money["net_premium"] + (0 if itm else 100 * strike)
    breakeven = (start if is_call else strike) - money["net_premium"] / 100
    lows = [decimal_price(stock["low"]) for stock in stock_path if _positive(stock.get("low"))]
    full_path = len(lows) == len(stock_path) and bool(lows)
    intrinsic = start - strike if is_call else Decimal(0)
    result = {
        **{
            key: float(value)
            for key, value in money.items()
            if key in {"pnl", "capital", "net_outlay", "net_premium", "return", "upside_forgone"}
        },
        **{
            f"{key}_decimal": str(money[key])
            for key in ("pnl", "capital", "net_outlay", "net_premium")
        },
        "baseline_return": float(money["benchmark_return"]),
        "haircut": haircut,
        "fee": 0.65,
        "entry_breakeven_cushion": float(1 - breakeven / start),
        "entry_time_value_yield": float((money["premium"] - intrinsic) / (money["capital"] / 100)),
        "entry_capped_return": float((money["net_premium"] - 100 * intrinsic) / money["capital"]),
        "ending_shares": shares,
        "ending_cash": float(cash),
        "ending_share_value": float(shares * close),
        "expiry_itm": itm,
        "expiry_atm": close == strike,
        "ending_state": ("atm_ambiguous" if close == strike else "shares" if shares else "cash"),
        "underlying_min_return": float(min(Decimal(0), min(lows) / start - 1))
        if full_path
        else None,
        "underlying_breakeven_breach": min(lows) < breakeven if full_path else None,
        "traded": True,
        "stock_slippage_bps": 0,
    }
    if is_call:
        for bps in (10, 25):
            buy = expiry_accounting(
                "call",
                strike,
                start,
                row["option_entry"],
                close,
                haircut=haircut,
                fee=0.65,
                stock_slippage_bps=bps,
            )
            result[f"buy_write_return_{bps}bp"] = float(buy["return"])
    return result


def _passive_economics(row: dict, stock_path: list[dict]) -> dict:
    """A known cancellation retains the starting wheel state without any fee."""
    start, strike, close = map(decimal_price,
                               (row["stock_entry"], row["strike"], row["expiry_close"]))
    is_call = row["side"] == "call"
    capital = 100 * (start if is_call else strike)
    pnl = 100 * (close - start) if is_call else Decimal(0)
    lows = [decimal_price(r["low"]) for r in stock_path if _positive(r.get("low"))]
    full_path = bool(lows) and len(lows) == len(stock_path)
    return {
        "traded": False, "pnl": float(pnl), "pnl_decimal": str(pnl),
        "capital": float(capital), "capital_decimal": str(capital),
        "net_outlay": float(capital), "net_outlay_decimal": str(capital),
        "net_premium": 0., "net_premium_decimal": "0", "fee": 0., "haircut": .10,
        "return": float(pnl / capital), "baseline_return": float(pnl / capital),
        "ending_shares": 100 if is_call else 0,
        "ending_cash": 0. if is_call else float(capital),
        "ending_share_value": float(100 * close) if is_call else 0.,
        "ending_state": "shares" if is_call else "cash", "stock_slippage_bps": 0,
        "entry_time_provenance": "not traded; cancelled at stock Open",
        "entry_breakeven_cushion": 0. if is_call else None,
        "underlying_min_return": (
            float(min(Decimal(0), min(lows) / start - 1)) if full_path and is_call else None
        ),
        "underlying_breakeven_breach": min(lows) < start if full_path and is_call else None,
    }


def _scoreable(row: dict) -> bool:
    return (isinstance(row.get("traded"), bool) and _finite(row.get("return"))
            and _finite(row.get("baseline_return")))


def _capture(days: pl.DataFrame, stocks: pl.DataFrame, output: Path) -> tuple[list[dict], dict]:
    """Stream all eligible contracts; retain only the small rule-opportunity panel."""
    calendar = SessionCalendar()
    features = stock_features(stocks)
    lookup = {(r["ticker"], r["ts"]): r for r in stocks.iter_rows(named=True)}
    actions, unknown, stock_sessions = defaultdict(list), defaultdict(set), defaultdict(set)
    for (ticker, session), stock in lookup.items():
        stock_sessions[ticker].add(session)
        dividend, split = stock.get("dividends"), stock.get("stock_splits")
        if not _finite(dividend) or not _finite(split):
            unknown[ticker].add(session)
        elif dividend or split:
            actions[ticker].append((session, dividend, split))
    sessions = calendar.sessions(days["session"].min(), days["expiry_session"].max())
    ordinal = {session: i for i, session in enumerate(sessions)}
    next_session = dict(zip(sessions[:-1], sessions[1:], strict=True))
    available = [max(stock_sessions[t]) for t in ("CIFR", "IREN", "WULF") if stock_sessions[t]]
    supplement_end = min([SUPPLEMENT_END, *available]) if len(available) == 3 else None
    writers = {
        name: _TableWriter(output / f"{name}.parquet", DETAIL_SCHEMA)
        for name in ("candidates", "opportunities", "cost_outcomes")
    }
    counts: Counter = Counter()
    opportunities = []
    # Aggregation at shared origin/expiry/cell gives large strike grids no extra date weight.
    descriptions: dict[tuple, dict] = {}
    try:
        for ticker in RESEARCH_TICKERS:
            frame = days.filter(pl.col("ticker") == ticker).sort(["session", "contract"])
            day_lookup = {(r["contract"], r["session"]): r for r in frame.iter_rows(named=True)}
            for (origin,), day in frame.group_by("session", maintain_order=True):
                decision = lookup.get((ticker, origin))
                chosen_groups: dict[tuple, list[dict]] = defaultdict(list)
                for raw in day.iter_rows(named=True):
                    counts["archive_contract_days"] += 1
                    if not decision or not _positive(decision["close"]):
                        counts["decision_missing_verified_stock"] += 1
                        continue
                    horizon = ordinal.get(raw["expiry_session"], -100) - ordinal.get(origin, 0)
                    band = horizon_band(horizon)
                    if band is None or origin not in next_session:
                        counts["outside_1_25_horizons"] += 1
                        continue
                    if (
                        not raw.get("valid", True)
                        or not _positive(raw.get("mark"))
                        or not _positive(raw.get("volume"))
                    ):
                        counts["invalid_decision_observation"] += 1
                        continue
                    depth = depth_band(decision["close"], raw["strike"])
                    if depth is None or raw["side"] not in {"call", "put"}:
                        counts["outside_strict_moneyness"] += 1
                        continue
                    entry = next_session[origin]
                    panel = _panel(ticker, origin, raw["expiry_session"], supplement_end)
                    row = {
                        **raw,
                        **features[(ticker, origin)],
                        **_decision_features(raw, decision["close"], entry),
                        "origin": origin,
                        "entry_session": entry,
                        "horizon": horizon,
                        "depth_band": depth,
                        "horizon_band": band,
                        "panel": panel,
                        "period": _period(panel, origin, raw["expiry_session"]),
                        "rule_id": f"{depth}/{band}",
                        "status": "scored",
                        "reason": None,
                        "opportunity_id": f"{ticker}:{origin}:{raw['contract']}",
                        "entry_time_provenance": "opening-hour first trade time unknown",
                    }
                    entry_stock = lookup.get((ticker, entry))
                    maturity_stock = lookup.get((ticker, raw["expiry_session"]))
                    entry_day = day_lookup.get((raw["contract"], entry))
                    row.update(
                        stock_entry=entry_stock["open"] if entry_stock else None,
                        option_entry=entry_day.get("opening_open") if entry_day else None,
                        expiry_close=maturity_stock["close"] if maturity_stock else None,
                        opening_start=entry_day.get("opening_start") if entry_day else None,
                        opening_end=entry_day.get("opening_end") if entry_day else None,
                    )
                    path_sessions = sessions[ordinal[entry] : ordinal[raw["expiry_session"]] + 1]
                    path = [
                        lookup[(ticker, session)]
                        for session in path_sessions
                        if (ticker, session) in lookup
                    ]
                    reason = _action_reason(
                        ticker,
                        origin,
                        entry,
                        raw["expiry_session"],
                        actions,
                        unknown,
                        stock_sessions,
                        path_sessions,
                    )
                    if not _positive(row["stock_entry"]):
                        row.update(status="no_entry", reason="missing_verified_stock_open")
                    elif decimal_price(raw["strike"]) >= decimal_price(row["stock_entry"]):
                        row.update(status="no_entry", reason="opening_moneyness_changed")
                    elif not _positive(row["option_entry"]):
                        row.update(status="no_entry", reason="missing_opening_bucket")
                    elif raw.get("reconciliation_failed") or (
                        entry_day
                        and (
                            not entry_day.get("valid", True)
                            or entry_day.get("reconciliation_failed")
                        )
                    ):
                        row.update(status="excluded", reason="reconciliation_failure")
                    elif not _positive(row["expiry_close"]):
                        row.update(status="excluded", reason="missing_verified_expiry_close")
                    elif reason:
                        row.update(status="excluded", reason=reason)
                    else:
                        row.update(_economics(row, path))
                    if row["reason"] == "opening_moneyness_changed":
                        passive_reason = reason
                        if not _positive(row["expiry_close"]):
                            passive_reason = "missing_verified_expiry_close"
                        if raw.get("reconciliation_failed") or (
                            entry_day and entry_day.get("reconciliation_failed")
                        ):
                            passive_reason = "reconciliation_failure"
                        if passive_reason:
                            row["outcome_unavailable_reason"] = passive_reason
                        else:
                            row.update(_passive_economics(row, path))
                    counts[f"candidate:{row['status']}:{row['reason'] or 'ok'}"] += 1
                    writers["candidates"].append(row)
                    chosen_groups[(raw["side"], depth, band)].append(row)
                    key = (
                        ticker,
                        raw["side"],
                        panel,
                        row["period"],
                        depth,
                        band,
                        origin,
                        raw["expiry_session"],
                    )
                    cell = descriptions.setdefault(
                        key, {"contracts": 0, "scored": 0, "returns": 0.0, "time_yields": 0.0,
                              "origin": origin, "expiry_session": raw["expiry_session"]}
                    )
                    cell["contracts"] += 1
                    cell["time_yields"] += row["decision_time_value_yield"]
                    if row["status"] == "scored":
                        cell["scored"] += 1
                        cell["returns"] += row["return"]
                for key in sorted(chosen_groups):
                    # Selection never inspects the entry/outcome fields attached above.
                    selected = select_wheel_contract(
                        chosen_groups[key], decision["close"], next_session[origin]
                    )
                    if selected is None:
                        continue
                    opportunities.append(selected)
                    writers["opportunities"].append(selected)
                    counts[f"selected:{selected['status']}:{selected['reason'] or 'ok'}"] += 1
                    if selected.get("traded") is False:
                        counts["selected_known_no_trade"] += 1
                    path = [
                        lookup[(ticker, session)]
                        for session in sessions[
                            ordinal[selected["entry_session"]] : ordinal[selected["expiry_session"]]
                            + 1
                        ]
                        if (ticker, session) in lookup
                    ]
                    for haircut in SETTINGS["premium_haircuts"]:
                        stressed = {**selected, "haircut": haircut, "fee": 0.65}
                        if selected["status"] == "scored":
                            stressed.update(_economics(selected, path, haircut))
                        elif selected.get("traded") is False:
                            stressed["fee"] = 0.
                        writers["cost_outcomes"].append(stressed)
    finally:
        for writer in writers.values():
            writer.close()
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for key, cell in sorted(descriptions.items()):
        grouped[key[:6]].append(cell)
    descriptive = []
    for key, cells in sorted(grouped.items()):
        scored = [cell["returns"] / cell["scored"] for cell in cells if cell["scored"]]
        descriptive.append(
            {
                **dict(
                    zip(
                        ("ticker", "side", "panel", "period", "depth_band", "horizon_band"),
                        key,
                        strict=True,
                    )
                ),
                "shared_origin_expiry_cells": len(cells),
                "origin_dates": len({cell["origin"] for cell in cells}),
                "expiries": len({cell["expiry_session"] for cell in cells}),
                "observed_contracts": sum(cell["contracts"] for cell in cells),
                "scored_contracts": sum(cell["scored"] for cell in cells),
                "mean_shared_cell_return": float(np.mean(scored)) if scored else None,
                "mean_shared_cell_time_value_yield": float(
                    np.mean([cell["time_yields"] / cell["contracts"] for cell in cells])
                ),
            }
        )
    counts["eligible_contract_days"] = writers["candidates"].count
    counts["fixed_opportunities"] = writers["opportunities"].count
    counts["fixed_cost_outcomes"] = writers["cost_outcomes"].count
    return opportunities, {
        "counts": dict(sorted(counts.items())),
        "all_contract_descriptives": descriptive,
    }


def _volatility_thresholds(rows: list[dict]) -> dict[str, list[float]]:
    unique = {
        (r["ticker"], r["origin"]): r["volatility20"]
        for r in rows
        if r["period"] == "development" and r["volatility20"] is not None
    }
    return {
        ticker: np.quantile(
            [v for (t, _), v in sorted(unique.items()) if t == ticker], [1 / 3, 2 / 3]
        ).tolist()
        for ticker in RESEARCH_TICKERS
        if any(t == ticker for t, _ in unique)
    }


def _condition(row: dict, condition: str | None, thresholds: dict) -> bool | None:
    if condition is None:
        return True
    if condition.startswith("volatility_"):
        value, cuts = row.get("volatility20"), thresholds.get(row["ticker"])
        if value is None or cuts is None:
            return None
        return {
            "volatility_low": value <= cuts[0],
            "volatility_middle": cuts[0] < value <= cuts[1],
            "volatility_high": value > cuts[1],
        }[condition]
    if condition.startswith("trend_"):
        value = row.get("return20")
        if value is None:
            return None
        return value > 0 if condition == "trend_positive" else value <= 0
    return row["decision_transactions"] >= 5 and row["decision_volume"] >= 20


def _conditional(rows: list[dict], condition: str | None, thresholds: dict) -> list[dict]:
    result = []
    for row in rows:
        passes = _condition(row, condition, thresholds)
        if passes is None:
            continue  # Unknown is not a deliberate zero-return decision.
        result.append(
            {**row, "traded": passes and row.get("traded", True),
             "return": row["return"] if passes else row["baseline_return"]}
        )
    return result


def _metrics(rows: list[dict]) -> dict:
    result = summarize(rows)
    result["weekday_counts"] = dict(
        sorted(Counter(r["origin"].strftime("%A") for r in rows).items())
    )
    result["expiry_counts"] = dict(
        sorted(Counter(r["expiry_session"].isoformat() for r in rows).items())
    )
    traded = [row for row in rows if row.get("traded", True)]
    result["participation"] = len(traded) / len(rows) if rows else None
    for name in (
        "decision_time_value_yield",
        "decision_breakeven_cushion",
        "entry_time_value_yield",
        "entry_breakeven_cushion",
        "underlying_min_return",
    ):
        values = [r[name] for r in traded if r.get(name) is not None]
        result[f"{name}_quartiles"] = (
            np.quantile(values, [0.25, 0.5, 0.75]).tolist() if values else None
        )
    result["expiry_itm_frequency"] = (
        float(np.mean([r["expiry_itm"] for r in traded])) if traded else None
    )
    result["traded_leg"] = summarize(traded)
    return result


def _entry_rates(rows: list[dict]) -> dict:
    total = len(rows)
    cancelled = sum(r.get("reason") == "opening_moneyness_changed" for r in rows)
    no_entry = sum(r["status"] == "no_entry" for r in rows)
    return {
        "no_entry_rate": no_entry / total if total else None,
        "known_cancellation_rate": cancelled / total if total else None,
        "missing_entry_rate": (no_entry - cancelled) / total if total else None,
    }


def _analyze(rows: list[dict], output: Path) -> dict:
    thresholds = _volatility_thresholds(rows)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["ticker"], row["side"], row["rule_id"], row["period"])].append(row)
    calendar = SessionCalendar()
    schedules = {
        "development": calendar.sessions(CORE_START, HOLDOUT - timedelta(days=1)),
        "holdout": calendar.sessions(HOLDOUT, CORE_END),
        "supplement": calendar.sessions(date(2026, 1, 1), SUPPLEMENT_END),
    }
    rule_results = []
    for key, opportunities in sorted(groups.items()):
        scored = [row for row in opportunities if _scoreable(row)]
        rule_results.append(
            {
                **dict(zip(("ticker", "side", "rule_id", "period"), key, strict=True)),
                "opportunities": len(opportunities),
                "status_counts": dict(sorted(Counter(r["status"] for r in opportunities).items())),
                **_entry_rates(opportunities),
                **_metrics(scored),
            }
        )
    reference = pl.read_parquet(
        output / "cost_outcomes.parquet",
        columns=["ticker", "side", "rule_id", "period", "haircut", "origin", "expiry_session",
                 "status", "return", "baseline_return", "assessment_days", "traded",
                 "volatility20", "return20", "decision_volume", "decision_transactions"],
    ).filter(pl.col("return").is_finite() & pl.col("baseline_return").is_finite()
             & pl.col("traded").is_not_null())
    cost_rows = defaultdict(list)
    for row in reference.iter_rows(named=True):
        cost_rows[
            (row["ticker"], row["side"], row["rule_id"], row["period"], row["haircut"])
        ].append(row)
    recommendations, screen_diagnostics, comparisons = [], [], []
    for ticker in RESEARCH_TICKERS:
        for side in ("call", "put"):
            candidates = [
                r
                for r in rule_results
                if r["ticker"] == ticker and r["side"] == side and r["period"] == "development"
            ]
            representatives = pareto_representatives(candidates)
            for preference in ("conservative", "balanced", "higher_return"):
                rule_id = representatives[preference]
                record = {
                    "ticker": ticker,
                    "side": side,
                    "preference": preference,
                    "rule_id": rule_id,
                    "condition": None,
                    "evidence": ["no_supported_positive_reward"]
                    if rule_id is None
                    else ["provisional"],
                }
                if rule_id is None:
                    recommendations.append(record)
                    continue
                selected_condition = None
                parent_dev = [
                    r
                    for r in groups[(ticker, side, rule_id, "development")]
                    if _scoreable(r)
                ]
                eligible_screens = []
                for condition in CONDITIONS:
                    conditional = _conditional(parent_dev, condition, thresholds)
                    matched_origins = {r["origin"] for r in conditional}
                    parent = [r for r in parent_dev if r["origin"] in matched_origins]
                    child_stats, parent_stats = summarize(conditional), summarize(parent)
                    active_stats = summarize([r for r in conditional if r["traded"]])
                    supported = active_stats["origin_dates"] >= 20 and active_stats["expiries"] >= 4
                    dominates = bool(parent) and (
                        (
                            child_stats["return_per_day"] > parent_stats["return_per_day"] + 1e-12
                            and child_stats["tail_loss"] <= parent_stats["tail_loss"]
                        )
                        or (
                            child_stats["tail_loss"] < parent_stats["tail_loss"] - 1e-12
                            and child_stats["return_per_day"] >= parent_stats["return_per_day"]
                        )
                    )
                    screen_diagnostics.append(
                        {
                            "ticker": ticker,
                            "side": side,
                            "rule_id": rule_id,
                            "condition": condition,
                            "supported": supported,
                            "dominates_parent": dominates,
                            "child": child_stats,
                            "matched_parent": parent_stats,
                        }
                    )
                    if supported and dominates:
                        eligible_screens.append(
                            (
                                (
                                    -child_stats["return_per_day"],
                                    child_stats["tail_loss"],
                                    condition,
                                ),
                                condition,
                            )
                        )
                if eligible_screens:
                    selected_condition = min(eligible_screens)[1]
                record.update(
                    condition=selected_condition,
                    depth_band=rule_id.split("/")[0],
                    horizon_band=rule_id.split("/")[1],
                )
                for period in schedules:
                    opportunities = groups.get((ticker, side, rule_id, period), [])
                    parent = [r for r in opportunities if _scoreable(r)]
                    policy = _conditional(parent, selected_condition, thresholds)
                    matched_dates = {r["origin"] for r in policy}
                    parent_matched = [r for r in parent if r["origin"] in matched_dates]
                    stats = _metrics(policy)
                    interval = moving_block_interval(
                        policy,
                        schedules[period],
                        paired_rows=parent_matched if selected_condition else None,
                    )
                    stats.update(
                        inference=interval,
                        opportunities=len(opportunities),
                        status_counts=dict(
                            sorted(Counter(r["status"] for r in opportunities).items())
                        ),
                        **_entry_rates(opportunities),
                        unavailable_condition=len(parent) - len(policy),
                    )
                    record[period] = stats
                validation = record["holdout"]
                if validation["inference"]["status"] != "estimable":
                    record["evidence"].append("underpowered")
                elif validation["return_per_day"] is not None:
                    record["evidence"].append(
                        "validation-consistent"
                        if validation["return_per_day"] > 0
                        else "validation-conflicted"
                    )
                record["holdout_direction"] = (
                    "unavailable"
                    if validation["return_per_day"] is None
                    else "positive_net_reward"
                    if validation["return_per_day"] > 0
                    else "nonpositive_net_reward"
                )
                sensitivities = []
                for period in schedules:
                    for haircut in SETTINGS["premium_haircuts"]:
                        policy = _conditional(
                            cost_rows.get((ticker, side, rule_id, period, haircut), []),
                            selected_condition,
                            thresholds,
                        )
                        sensitivities.append(
                            {"period": period, "haircut": haircut, **summarize(policy)}
                        )
                record["cost_sensitivity"] = sensitivities
                development_returns = [
                    r["return_per_day"] for r in sensitivities if r["period"] == "development"
                ]
                record["cost_sensitive_reward_sign"] = any(
                    v is not None and v <= 0 for v in development_returns
                ) and any(v is not None and v > 0 for v in development_returns)
                recommendations.append(record)
            # Disclose intersections; disjoint expiry weekdays cannot be paired.
            for i, left in enumerate(candidates):
                for right in candidates[i + 1 :]:
                    a = {
                        r["origin"]: r
                        for r in groups[(ticker, side, left["rule_id"], "development")]
                        if _scoreable(r)
                    }
                    b = {
                        r["origin"]: r
                        for r in groups[(ticker, side, right["rule_id"], "development")]
                        if _scoreable(r)
                    }
                    dates = sorted(a.keys() & b.keys())
                    comparisons.append(
                        {
                            "ticker": ticker,
                            "side": side,
                            "left": left["rule_id"],
                            "right": right["rule_id"],
                            "matched_dates": len(dates),
                            "left_metrics": summarize([a[d] for d in dates]),
                            "right_metrics": summarize([b[d] for d in dates]),
                            "inference": moving_block_interval(
                                [a[d] for d in dates], schedules["development"],
                                paired_rows=[b[d] for d in dates],
                            ),
                        }
                    )
    return {
        "volatility_thresholds": thresholds,
        "rules": rule_results,
        "recommendations": recommendations,
        "screen_diagnostics": screen_diagnostics,
        "matched_comparisons": comparisons,
    }


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.2f}%"


def _condition_description(condition: str | None, ticker: str, thresholds: dict) -> str:
    if condition is None:
        return "No additional condition"
    if condition.startswith("volatility_"):
        low, high = (f"{100 * cut:.8g}%" for cut in thresholds[ticker])
        ranges = {"volatility_low": f"<= {low}",
                  "volatility_middle": f"> {low} and <= {high}",
                  "volatility_high": f"> {high}"}
        return "Annualized 20-session realized volatility " + ranges[condition]
    if condition == "trend_positive":
        return "20-session return > 0% (stock)"
    if condition == "trend_nonpositive":
        return "20-session return <= 0% (stock)"
    return "Decision-session activity: at least 5 transactions and 20 contracts of volume"


def _write_reports(output: Path, summary: dict) -> None:
    lines = [
        "# Next wheel-leg selling checklist",
        "",
        "Owned shares → ITM covered call; cash → OTM cash-secured put. Original cost basis and",
        "existing unrealized P&L are ignored. Positions are assessed at expiry, including retained",
        'shares at market value. These are provisional historical preference examples, '
        'not proven optima.',
        "",
        'Reference: 10% total-premium haircut, $0.65 fee, zero cash yield. Capital is '
        'current stock',
        "market value for calls and strike collateral for puts. Return/day uses inclusive calendar",
        'dates from entry to expiry and does not imply capital release or repeatable '
        'annual returns.',
        "",
        '| Ticker | Leg | Preference | Depth below spot | Trading sessions | Condition '
        '| Dev net/day | Dev worst-5% | Holdout net/day | 2026 net/day | Evidence |',
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for rec in summary["recommendations"]:
        if rec["rule_id"] is None:
            cells = [rec["ticker"], rec["side"], rec["preference"], "No supported sale",
                     *["—"] * 6, "No supported positive reward"]
            lines.append("| " + " | ".join(cells) + " |")
            continue
        dev, held, supplement = (rec[p] for p in ("development", "holdout", "supplement"))
        cells = [rec["ticker"], rec["side"], rec["preference"], rec["depth_band"] + "%",
                 rec["horizon_band"], _condition_description(
                     rec["condition"], rec["ticker"], summary["volatility_thresholds"]),
                 _percent(dev["return_per_day"]), _percent(dev["expected_shortfall05"]),
                 _percent(held["return_per_day"]), _percent(supplement["return_per_day"]),
                 ", ".join(rec["evidence"]) + "; holdout " + rec["holdout_direction"]]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "## How to use the characteristics",
        "",
        "Choose the ticker and current wheel state. Treat depth/expiry bands as candidate filters,",
        'then compare time-value reward, breakeven protection, observed activity and '
        'the cost cases.',
        "Skip rather than replace a prior-close choice if the next stock Open crosses its strike.",
        "Missing opening bars are unknown entries, not zero premiums. If no supported positive",
        "development choice exists, the research supplies no supported selling recommendation.",
        "",
    ]
    for rec in summary["recommendations"]:
        if rec["rule_id"] is None:
            continue
        lines += [f"### {rec['ticker']} {rec['side']} — {rec['preference']}", ""]
        lines.append("- Condition: " + _condition_description(
            rec["condition"], rec["ticker"], summary["volatility_thresholds"]) + ".")
        for period in ("development", "holdout", "supplement"):
            stats = rec[period]
            lines.append(
                f"- {period}: {stats['origin_dates']} scored dates, "
                f"{stats['expiries']} expiries; traded {stats['traded']}; "
                f"loss frequency {_percent(stats['loss_frequency'])}; "
                f"fifth percentile {_percent(stats['quantile05'])}; "
                f"participation {_percent(stats['participation'])}; "
                f"missing-entry rate {_percent(stats['missing_entry_rate'])}; "
                f"known opening cancellation rate "
                f"{_percent(stats.get('known_cancellation_rate'))}."
            )
        dev = rec["development"]
        for name, title in (
            ("decision_time_value_yield", "Decision time-value yield"),
            ("decision_breakeven_cushion", "Decision breakeven cushion"),
        ):
            quartiles = dev[f"{name}_quartiles"]
            lines.append(
                f"- {title}, development 25th/median/75th percentiles: "
                + (" / ".join(_percent(v) for v in quartiles) if quartiles else "unavailable")
                + ". These are observed ranges, not optimized eligibility thresholds."
            )
        lines.append(
            "- Development net/day at 0/5/10/20% total-premium haircuts: "
            + " / ".join(
                _percent(s["return_per_day"])
                for s in rec["cost_sensitivity"]
                if s["period"] == "development"
            )
            + "."
        )
        if rec["cost_sensitive_reward_sign"]:
            lines.append(
                '- Cost-sensitive reward sign: the apparent benefit depends on the assumed '
                'premium deduction.'
            )
        lines.append("")
    lines += [
        "## Interpretation limits",
        "",
        'Expiry-close ITM is a terminal exercise proxy. Actual/early assignments and '
        'settlement times',
        'are unknown; ATM outcomes are ambiguous. Opening-hour prices are trade proxies'
        ' with unknown',
        'first-trade times, not executable quotes or synchronous stock/option fills. '
        'ITM call haircuts',
        'also reduce intrinsic value, so compare assumptions before interpreting a '
        'strike sweet spot.',
        "No observed historical IV, delta, spreads, OI or earnings filters enter this study.",
        "",
        "Conditions are learned on development data only. Failed conditions retain stock exposure",
        "for calls and cash for puts. Unknown outcomes remain missing.",
        "Known opening-moneyness cancellations also retain stock/cash wealth without option fees.",
        "These enter whole-opportunity metrics; missing opening prices remain unknown.",
        "All holdout choices stay frozen.",
        "Different rule coverage and expiry weekdays can prevent fair pairing; those intersections",
        'are reported explicitly. Interim diagnostics describe stock lows, not '
        'option-position drawdown.',
        'NBIS has no verified 2026 confirmation. Long-horizon and narrow-cell evidence '
        'may be underpowered.',
        "",
    ]
    (output / "checklist.md").write_text("\n".join(lines))
    rule_lines = [
        "## Complete development rule comparisons", "",
        "One selected contract per origin/rule. These availability-set descriptions",
        "are not matched winners; summary.json records every paired-date intersection.", "",
        "| Ticker | Leg | Depth/expiry rule | Dates | Expiries | Net/day | Mean expiry return | "
        "Worst-5% return | Loss rate | Missing entry |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for rule in summary["rules"]:
        if rule["period"] != "development":
            continue
        cells = [rule["ticker"], rule["side"], rule["rule_id"], str(rule["origin_dates"]),
                 str(rule["expiries"]), _percent(rule["return_per_day"]),
                 _percent(rule["mean_return"]), _percent(rule["expected_shortfall05"]),
                 _percent(rule["loss_frequency"]), _percent(rule["missing_entry_rate"])]
        rule_lines.append("| " + " | ".join(cells) + " |")
    report = [
        "# Wheel-characteristic research",
        "",
        "## Coverage",
        "",
        "```json",
        json.dumps(summary["counts"], sort_keys=True, indent=2),
        "```",
        "",
        "## Method",
        "",
        'All eligible observed contracts are described before one deterministic '
        'contract is selected',
        'per ticker/origin/depth/expiry band. Selection maximizes decision-only '
        'stressed capped gain',
        "per gross-capital assessment day; ties use transactions, volume, strike and identifier.",
        'Development outcomes mature strictly before 2025-09-17. The 2025 holdout and '
        'separate 2026',
        'supplement never select or retune rules. Pareto examples trade reward/day '
        'against worst-5% loss.',
        "At most one preset condition can be attached after matched development dominance.",
        'Moving date-block uncertainty uses 26 sessions, 2000 draws, seed 1729, at '
        'least20 scored dates',
        'and eight supported complete blocks. All comparisons are exploratory; raw '
        'availability-set',
        "frontiers are provisional, not matched universal winners.",
        "All-contract descriptive means average shared origin/expiry cells; selected-rule",
        "metrics have one observation per origin. Neither contract counts nor overlapping",
        "expiry labels establish independent statistical support.",
        "",
        *rule_lines, "",
        "## Selling checklist",
        "",
        *lines[2:],
        "",
        "## Artifacts",
        "",
        'candidates.parquet retains eligible observed contracts. opportunities.parquet '
        'retains fixed',
        'selections and every failure status. cost_outcomes.parquet contains the same '
        'selections across',
        'cost assumptions, including secondary buy-write purchase-slippage results. '
        'summary.json holds',
        "all rule metrics, matched-date comparisons, frozen choices and condition diagnostics.",
        "",
    ]
    (output / "report.md").write_text("\n".join(report))


def run_wheel_frames(days: pl.DataFrame, stocks: pl.DataFrame, output: Path) -> dict:
    """Frame-level engine for synthetic tests; command publication wraps this."""
    if days.is_empty() or stocks.is_empty():
        raise ValueError("wheel research requires option and stock observations")
    output.mkdir(parents=True, exist_ok=True)
    rows, coverage = _capture(days, stocks, output)
    print(f"Wheel: {len(rows)} fixed opportunities; analyzing characteristics", flush=True)
    summary = {"version": 1, "settings": SETTINGS, **coverage, **_analyze(rows, output)}
    (output / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2, allow_nan=False) + "\n"
    )
    _write_reports(output, summary)
    return summary


def run_wheel(snapshot: Path, output: Path) -> dict:
    started = perf_counter()
    snapshot, output = safe_path(snapshot, must_exist=True), safe_path(output)
    if output == snapshot or output.is_relative_to(snapshot) or snapshot.is_relative_to(output):
        raise ValueError("wheel output and frozen input paths must not overlap")
    manifest = verify_snapshot(snapshot)
    with atomic_directory(output) as stage:
        days = pl.read_parquet(
            snapshot / "days.parquet",
            columns=["ticker", "contract", "session", "expiry", "expiry_session", "side",
                     "strike", "mark", "volume", "transactions", "regular_hours", "valid",
                     "reason", "reconciliation_failed", "opening_open", "opening_start",
                     "opening_end"],
        )
        stocks = pl.read_parquet(snapshot / "stocks.parquet")
        summary = run_wheel_frames(days, stocks, stage)
        summary["snapshot_hash"] = manifest["canonical_hash"]
        (stage / "summary.json").write_text(
            json.dumps(summary, sort_keys=True, indent=2, allow_nan=False) + "\n"
        )
        runtime = {
            "duration_seconds": perf_counter() - started,
            "peak_rss_bytes": peak_rss_bytes(),
            "memory_target_bytes": 4 * 1024**3,
        }
        (stage / "resource.json").write_text(json.dumps(runtime, sort_keys=True, indent=2) + "\n")
        return finalize_manifest(
            stage,
            {
                "version": 1,
                "kind": "wheel-characteristics-run",
                "snapshot_hash": manifest["canonical_hash"],
                "settings": SETTINGS,
                "code": environment_metadata(),
                "environment": environment_versions(),
                "runtime": runtime,
            },
        )
