"""Read and write sweep results."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from stocksweeper.storage.db import connect, rows
from stocksweeper.strategy.generator import present_signals

# Parameter text matches _format_parameters: keys sorted, joined as key=value.
# The test segment is reported and is intentionally absent from this whitelist.
_PARAMETER_SORT = """(
    SELECT coalesce(
        string_agg(
            k || '=' || json_extract_string(
                coalesce(json_extract(s.definition_json, '$.parameters'), '{}'),
                '$.' || k
            ),
            ', ' ORDER BY k
        ),
        ''
    )
    FROM unnest(
        json_keys(coalesce(json_extract(s.definition_json, '$.parameters'), '{}'))
    ) AS t(k)
)"""

SORTS = {
    "rank": "rb.rank",
    "ticker": "rb.ticker",
    "strategy": "s.name",
    "signals": "s.signals",
    "parameters": _PARAMETER_SORT,
    "robustness": "rb.score",
    "oos_cagr": "v.cagr",
    "sharpe": "v.sharpe",
    "max_drawdown": "v.max_drawdown",
    "win_rate": "v.win_rate",
    "trades": "v.n_trades",
}

# Outer aliases after grouping. test_cagr is omitted on purpose.
GROUP_SORTS = {
    "rank": "rank",
    "ticker": "ticker",
    "strategy": "strategy",
    "signals": "signals",
    "parameters": "parameter_sort",
    "robustness": "robustness",
    "oos_cagr": "oos_cagr",
    "sharpe": "sharpe",
    "max_drawdown": "max_drawdown",
    "win_rate": "win_rate",
    "trades": "trades",
}


class Repository:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "results.duckdb"

    def save_sweep(self, payload: dict[str, Any]) -> None:
        with connect(self.path) as connection:
            connection.execute("BEGIN")
            try:
                _insert(connection, "runs", payload["runs"])
                _insert(connection, "strategies", payload["strategies"])
                _insert(connection, "results", payload["results"])
                _insert(connection, "robustness", payload["robustness"])
                _insert(connection, "walk_forward", payload["walk_forward"])
                _insert(connection, "walk_forward_reopt", payload["reopt"])
                _insert(connection, "cross_ticker", payload["cross_ticker"])
                _insert(connection, "benchmarks", payload["benchmarks"])
                _insert(connection, "run_tickers", payload.get("run_tickers", []))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def list_runs(self) -> list[dict[str, Any]]:
        with connect(self.path) as connection:
            return rows(connection, "SELECT * FROM runs ORDER BY created_at DESC")

    def latest_run_id(self) -> str | None:
        found = self.list_runs()
        if not found:
            return None
        return str(found[0]["id"])

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with connect(self.path) as connection:
            found = rows(connection, "SELECT * FROM runs WHERE id = ?", [run_id])
        return found[0] if found else None

    def overview(self, run_id: str, tickers: list[str]) -> list[dict[str, Any]]:
        with connect(self.path) as connection:
            best = rows(
                connection,
                """
                SELECT rb.ticker, rb.strategy_id, rb.score, rb.rank, s.name, s.signals,
                       s.definition_json,
                       v.cagr, v.sharpe, v.max_drawdown, v.n_trades,
                       t.cagr AS test_cagr, t.max_drawdown AS test_max_drawdown,
                       t.n_trades AS test_trades
                FROM robustness rb
                JOIN strategies s ON s.id = rb.strategy_id
                LEFT JOIN results v
                  ON v.run_id = rb.run_id AND v.strategy_id = rb.strategy_id
                 AND v.ticker = rb.ticker AND v.segment = 'validation'
                LEFT JOIN results t
                  ON t.run_id = rb.run_id AND t.strategy_id = rb.strategy_id
                 AND t.ticker = rb.ticker AND t.segment = 'test'
                WHERE rb.run_id = ? AND rb.rank = 1
                """,
                [run_id],
            )
            benches = rows(
                connection,
                """
                SELECT ticker, segment, cagr, max_drawdown
                FROM benchmarks
                WHERE run_id = ? AND segment IN ('validation', 'test')
                """,
                [run_id],
            )
        by_ticker = {str(row["ticker"]): row for row in best}
        bench_by = {(str(row["ticker"]), str(row["segment"])): row for row in benches}
        cards = []
        for ticker in tickers:
            row = by_ticker.get(ticker, {})
            bench = bench_by.get((ticker, "validation"), {})
            hold = bench_by.get((ticker, "test"), {})
            entry_signals, filter_signals, exit_signals, exit_kind = _presented(row)
            cards.append(
                {
                    "ticker": ticker,
                    "strategy_id": row.get("strategy_id"),
                    "strategy_name": row.get("name"),
                    "signals": row.get("signals"),
                    "entry_signals": entry_signals,
                    "filter_signals": filter_signals,
                    "exit_signals": exit_signals,
                    "exit_kind": exit_kind,
                    "robustness": row.get("score"),
                    "oos_cagr": row.get("cagr"),
                    "sharpe": row.get("sharpe"),
                    "max_drawdown": row.get("max_drawdown"),
                    "trades": row.get("n_trades"),
                    "buy_hold_cagr": bench.get("cagr"),
                    "buy_hold_max_drawdown": bench.get("max_drawdown"),
                    "test_cagr": row.get("test_cagr"),
                    "test_max_drawdown": row.get("test_max_drawdown"),
                    "test_trades": row.get("test_trades"),
                    "test_buy_hold_cagr": hold.get("cagr"),
                }
            )
        return cards

    def leaderboard(
        self,
        run_id: str,
        *,
        ticker: str | None,
        family: str | None,
        min_trades: int | None,
        include_rejected: bool,
        exit_kind: str | None = None,
        filter_count: int | None = None,
        sort: str,
        order: str,
        limit: int,
        offset: int,
        group_variants: bool = False,
        rule: str | None = None,
    ) -> dict[str, Any]:
        column = SORTS.get(sort, "rb.score")
        direction = "ASC" if order == "asc" else "DESC"
        filters = ["rb.run_id = ?"]
        params: list[object] = [run_id]
        if not include_rejected:
            filters.append("NOT rb.rejected")
        if ticker:
            filters.append("rb.ticker = ?")
            params.append(ticker)
        if family:
            filters.append("s.family = ?")
            params.append(family)
        if min_trades is not None:
            filters.append("v.n_trades >= ?")
            params.append(min_trades)
        if exit_kind:
            filters.append(
                "COALESCE(json_extract_string(s.definition_json, '$.exit_rule.kind'), 'mirror') = ?"
            )
            params.append(exit_kind)
        if filter_count is not None:
            filters.append(
                "COALESCE(json_array_length(json_extract(s.definition_json, '$.filters')), 0) = ?"
            )
            params.append(filter_count)
        if rule:
            filters.append("s.name = ?")
            params.append(rule)
        where = " AND ".join(filters)
        base = f"""
            FROM robustness rb
            JOIN strategies s ON s.id = rb.strategy_id
            LEFT JOIN results v
              ON v.run_id = rb.run_id AND v.strategy_id = rb.strategy_id
             AND v.ticker = rb.ticker AND v.segment = 'validation'
            LEFT JOIN results t
              ON t.run_id = rb.run_id AND t.strategy_id = rb.strategy_id
             AND t.ticker = rb.ticker AND t.segment = 'test'
            WHERE {where}
        """
        with connect(self.path) as connection:
            if group_variants:
                total, items = _grouped_page(
                    connection, base, params, sort, direction, limit, offset
                )
            else:
                total = rows(connection, f"SELECT COUNT(*) AS n {base}", params)[0]["n"]
                items = rows(
                    connection,
                    f"""
                    SELECT rb.rank, rb.ticker, s.id AS strategy_id, s.name AS strategy, s.family,
                           s.signals, s.definition_json, rb.score AS robustness, rb.rejected,
                           rb.flags_json, v.cagr AS oos_cagr, v.sharpe, v.max_drawdown,
                           v.win_rate, v.n_trades AS trades, t.cagr AS test_cagr
                    {base}
                    ORDER BY {column} {direction} NULLS LAST
                    LIMIT ? OFFSET ?
                    """,
                    [*params, limit, offset],
                )
            families = rows(
                connection,
                """
                SELECT DISTINCT s.family AS family
                FROM robustness rb JOIN strategies s ON s.id = rb.strategy_id
                WHERE rb.run_id = ? ORDER BY 1
                """,
                [run_id],
            )
        for item in items:
            definition = json.loads(str(item.pop("definition_json")))
            item.pop("parameter_sort", None)
            item["parameters"] = _format_parameters(definition.get("parameters", {}))
            item["flags"] = json.loads(str(item.pop("flags_json") or "[]"))
            item.setdefault("variants", 1)
            _attach_signals(item, definition)
        return {
            "total": int(total) if isinstance(total, int | float) else 0,
            "families": [str(row["family"]) for row in families],
            "items": items,
        }

    def cross_ticker(self, run_id: str) -> list[dict[str, Any]]:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """
                SELECT c.strategy_id, s.name AS strategy, s.family, s.signals, s.definition_json,
                       c.cross_score, c.per_ticker_json
                FROM cross_ticker c
                JOIN strategies s ON s.id = c.strategy_id
                WHERE c.run_id = ? AND c.qualified
                ORDER BY c.cross_score DESC
                """,
                [run_id],
            )
        for item in found:
            definition = json.loads(str(item.pop("definition_json")))
            item["parameters"] = _format_parameters(definition.get("parameters", {}))
            item["per_ticker"] = json.loads(str(item.pop("per_ticker_json")))
            _attach_signals(item, definition)
        return found

    def run_tickers(self, run_id: str) -> list[dict[str, Any]]:
        with connect(self.path) as connection:
            return rows(
                connection,
                """
                SELECT ticker, n_bars, first_ts, last_ts, bars_hash, limited_history, survivors
                FROM run_tickers
                WHERE run_id = ?
                ORDER BY ticker
                """,
                [run_id],
            )

    def ticker_snapshot(self, run_id: str, ticker: str) -> dict[str, Any] | None:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """
                SELECT ticker, n_bars, first_ts, last_ts, bars_hash, limited_history, survivors
                FROM run_tickers
                WHERE run_id = ? AND ticker = ?
                """,
                [run_id, ticker],
            )
        return found[0] if found else None

    def benchmarks(self, run_id: str, ticker: str) -> dict[str, dict[str, Any]]:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """
                SELECT segment, cagr, max_drawdown
                FROM benchmarks
                WHERE run_id = ? AND ticker = ?
                """,
                [run_id, ticker],
            )
        return {str(row["segment"]): row for row in found}

    def strategy_record(self, run_id: str, strategy_id: str, ticker: str) -> dict[str, Any] | None:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """
                SELECT s.definition_json, s.name, s.family, s.signals, rb.score, rb.flags_json,
                       rb.rejected, rb.rank, rb.degradation, rb.stability_component,
                       rb.walk_forward_component
                FROM strategies s
                JOIN robustness rb ON rb.strategy_id = s.id
                WHERE rb.run_id = ? AND s.id = ? AND rb.ticker = ?
                """,
                [run_id, strategy_id, ticker],
            )
            if not found:
                return None
            record = found[0]
            record["segments"] = rows(
                connection,
                """
                SELECT * FROM results
                WHERE run_id = ? AND strategy_id = ? AND ticker = ?
                ORDER BY segment
                """,
                [run_id, strategy_id, ticker],
            )
            record["folds"] = rows(
                connection,
                """
                SELECT fold, is_sharpe, oos_sharpe, oos_return
                FROM walk_forward
                WHERE run_id = ? AND strategy_id = ? AND ticker = ?
                ORDER BY fold
                """,
                [run_id, strategy_id, ticker],
            )
            family = record["family"]
            record["reopt"] = rows(
                connection,
                """
                SELECT fold, strategy_id, oos_sharpe, oos_return
                FROM walk_forward_reopt
                WHERE run_id = ? AND ticker = ? AND family = ?
                ORDER BY fold
                """,
                [run_id, ticker, family],
            )
        return record


def _insert(connection: Any, table: str, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    frame = pl.from_dicts(records)
    connection.register("_batch", frame.to_arrow())
    columns = ", ".join(frame.columns)
    connection.execute(f"INSERT OR REPLACE INTO {table} ({columns}) SELECT {columns} FROM _batch")
    connection.unregister("_batch")


def _grouped_page(
    connection: Any,
    base: str,
    params: list[object],
    sort: str,
    direction: str,
    limit: int,
    offset: int,
) -> tuple[object, list[dict[str, Any]]]:
    order_column = GROUP_SORTS.get(sort, "robustness")
    filtered = f"""
        filtered AS (
            SELECT rb.rank, rb.ticker, s.id AS strategy_id, s.name AS strategy, s.family,
                   s.signals, s.definition_json, rb.score AS robustness, rb.rejected,
                   rb.flags_json, v.cagr AS oos_cagr, v.sharpe, v.max_drawdown,
                   v.win_rate, v.n_trades AS trades, t.cagr AS test_cagr,
                   {_PARAMETER_SORT} AS parameter_sort
            {base}
        ),
        ranked AS (
            SELECT *,
                   COUNT(*) OVER (PARTITION BY ticker, strategy) AS variants,
                   ROW_NUMBER() OVER (
                       PARTITION BY ticker, strategy
                       ORDER BY robustness DESC NULLS LAST, strategy_id
                   ) AS rn
            FROM filtered
        )
    """
    total = rows(
        connection,
        f"WITH {filtered} SELECT COUNT(*) AS n FROM ranked WHERE rn = 1",
        params,
    )[0]["n"]
    items = rows(
        connection,
        f"""
        WITH {filtered}
        SELECT * EXCLUDE (rn)
        FROM ranked
        WHERE rn = 1
        ORDER BY {order_column} {direction} NULLS LAST, strategy_id
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    return total, items


def _presented(row: dict[str, Any]) -> tuple[list[str], list[str], list[str], str]:
    definition: dict[str, Any] = {}
    raw = row.get("definition_json")
    if raw:
        loaded = json.loads(str(raw))
        if isinstance(loaded, dict):
            definition = loaded
    rule = definition.get("exit_rule")
    kind = rule.get("kind") if isinstance(rule, dict) else None
    return present_signals(str(row.get("signals") or ""), definition.get("filters"), kind)


def _attach_signals(item: dict[str, Any], definition: dict[str, Any] | None = None) -> None:
    body = definition or {}
    rule = body.get("exit_rule")
    kind = rule.get("kind") if isinstance(rule, dict) else None
    entry_signals, filter_signals, exit_signals, exit_kind = present_signals(
        str(item.get("signals") or ""), body.get("filters"), kind
    )
    item["entry_signals"] = entry_signals
    item["filter_signals"] = filter_signals
    item["exit_signals"] = exit_signals
    item["exit_kind"] = exit_kind


def _format_parameters(parameters: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(parameters.items()))


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
