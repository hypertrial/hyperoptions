import json
from typing import Any

from stocksweeper.storage.repo import GROUP_SORTS, SORTS, Repository, utc_now
from stocksweeper.strategy.model import ConditionGroup, Strategy, cond, const, content_id


def _strategy() -> Strategy:
    entry = ConditionGroup(logic="AND", conditions=[cond("close", "gt", const(1))])
    exit_group = ConditionGroup(logic="OR", conditions=[cond("close", "lt", const(1))])
    return Strategy(
        id=content_id(
            family="trend_momentum",
            entry=entry,
            exit_group=exit_group,
            parameters={"ema_stack_fast": 10},
        ),
        name="Example",
        family="trend_momentum",
        entry=entry,
        exit=exit_group,
        parameters={"ema_stack_fast": 10},
        signals="Close>1 | Close<1",
    )


def test_round_trip_leaderboard_orders_by_robustness(tmp_path):
    strategy = _strategy()
    repo = Repository(tmp_path)
    repo.save_sweep(
        {
            "runs": [
                {
                    "id": "run1",
                    "created_at": utc_now(),
                    "config_json": "{}",
                    "status": "completed",
                    "strategy_count": 1,
                    "ticker_count": 1,
                }
            ],
            "strategies": [
                {
                    "id": strategy.id,
                    "name": strategy.name,
                    "family": strategy.family,
                    "definition_json": strategy.model_dump_json(),
                    "signals": strategy.signals,
                }
            ],
            "results": [
                _result(strategy.id, "validation", 0.2, 1.5, 12),
                _result(strategy.id, "test", 0.9, 3.0, 4),
            ],
            "robustness": [
                {
                    "run_id": "run1",
                    "strategy_id": strategy.id,
                    "ticker": "IREN",
                    "score": 42.0,
                    "sharpe_component": 0.4,
                    "cagr_component": 0.2,
                    "drawdown_component": 0.5,
                    "profit_factor_component": 0.3,
                    "trade_component": 0.4,
                    "walk_forward_component": 0.5,
                    "stability_component": 1.0,
                    "degradation": 0.8,
                    "flags_json": json.dumps([]),
                    "rejected": False,
                    "rank": 1,
                }
            ],
            "walk_forward": [],
            "reopt": [],
            "cross_ticker": [],
            "benchmarks": [
                {
                    "run_id": "run1",
                    "ticker": "IREN",
                    "segment": "validation",
                    "cagr": 0.05,
                    "total_return": 0.05,
                    "sharpe": 0.4,
                    "sortino": 0.4,
                    "max_drawdown": -0.1,
                    "calmar": 0.5,
                }
            ],
        }
    )
    board = repo.leaderboard(
        "run1",
        ticker=None,
        family=None,
        min_trades=None,
        include_rejected=False,
        sort="robustness",
        order="desc",
        limit=10,
        offset=0,
    )
    assert board["total"] == 1
    assert board["items"][0]["robustness"] == 42
    assert board["items"][0]["test_cagr"] == 0.9
    assert board["items"][0]["oos_cagr"] == 0.2
    assert "ema_stack_fast=10" in board["items"][0]["parameters"]
    overview = repo.overview("run1", ["IREN"])
    assert overview[0]["strategy_name"] == "Example"
    assert overview[0]["buy_hold_cagr"] == 0.05
    assert overview[0]["oos_cagr"] == 0.2
    assert overview[0]["test_cagr"] == 0.9
    assert overview[0]["test_trades"] == 4
    assert overview[0]["test_buy_hold_cagr"] is None
    assert overview[0]["entry_signals"] == ["Close>1"]
    assert overview[0]["filter_signals"] == []
    assert overview[0]["exit_signals"] == ["Close<1"]
    assert overview[0]["exit_kind"] == "mirror"
    assert board["items"][0]["entry_signals"] == ["Close>1"]
    assert board["items"][0]["filter_signals"] == []
    assert board["items"][0]["exit_kind"] == "mirror"


def test_legacy_definitions_keep_one_entry_line_and_grammar_rules_split(tmp_path):
    legacy = _strategy()
    body = json.loads(legacy.model_dump_json())
    body.pop("exit_rule")
    body.pop("filters")
    body.pop("signal")
    body["signals"] = "Close>1 & EMA(20)>EMA(50) | Close<1"
    grammar = _strategy()
    grammar_body = json.loads(grammar.model_dump_json())
    grammar_body["id"] = "grammar1"
    grammar_body["filters"] = ["ema_filter"]
    grammar_body["signal"] = "rsi_pullback"
    grammar_body["exit_rule"] = {"kind": "time", "atr_length": 14, "atr_mult": 3.0, "bars": 10}
    grammar_body["signals"] = "RSI(14)<30 & EMA(20)>EMA(50) | Time stop 10 bars"
    grammar_two = _strategy()
    grammar_two_body = json.loads(grammar_two.model_dump_json())
    grammar_two_body["id"] = "grammar2"
    grammar_two_body["filters"] = ["ema_filter", "cmf_filter"]
    grammar_two_body["signal"] = "rsi_pullback"
    grammar_two_body["exit_rule"] = {
        "kind": "atr_trail",
        "atr_length": 14,
        "atr_mult": 3.0,
        "bars": 10,
    }
    grammar_two_body["signals"] = "RSI(14)<30 & EMA(20)>EMA(50) & CMF(20)>0 | ATR(14) trail 3x"
    repo = Repository(tmp_path)
    repo.save_sweep(
        {
            "runs": [
                {
                    "id": "run1",
                    "created_at": utc_now(),
                    "config_json": "{}",
                    "status": "completed",
                    "strategy_count": 3,
                    "ticker_count": 1,
                }
            ],
            "strategies": [
                {
                    "id": legacy.id,
                    "name": "Legacy",
                    "family": "trend_momentum",
                    "definition_json": json.dumps(body),
                    "signals": body["signals"],
                },
                {
                    "id": "grammar1",
                    "name": "RSI + EMA filter, time stop",
                    "family": "momentum_pullback",
                    "definition_json": json.dumps(grammar_body),
                    "signals": grammar_body["signals"],
                },
                {
                    "id": "grammar2",
                    "name": "RSI + EMA + CMF, ATR trail",
                    "family": "momentum_pullback",
                    "definition_json": json.dumps(grammar_two_body),
                    "signals": grammar_two_body["signals"],
                },
            ],
            "results": [
                _result(legacy.id, "validation", 0.1, 1.0, 8),
                _result("grammar1", "validation", 0.2, 1.2, 9),
                _result("grammar2", "validation", 0.15, 1.1, 7),
            ],
            "robustness": [
                _robust(legacy.id, 10.0, 2),
                _robust("grammar1", 20.0, 1),
                _robust("grammar2", 15.0, 3),
            ],
            "walk_forward": [],
            "reopt": [],
            "cross_ticker": [],
            "benchmarks": [],
        }
    )
    board = repo.leaderboard(
        "run1",
        ticker=None,
        family=None,
        min_trades=None,
        include_rejected=True,
        sort="robustness",
        order="desc",
        limit=10,
        offset=0,
    )
    by_id = {item["strategy_id"]: item for item in board["items"]}
    assert by_id[legacy.id]["entry_signals"] == ["Close>1", "EMA(20)>EMA(50)"]
    assert by_id[legacy.id]["filter_signals"] == []
    assert by_id[legacy.id]["exit_kind"] == "mirror"
    assert by_id["grammar1"]["entry_signals"] == ["RSI(14)<30"]
    assert by_id["grammar1"]["filter_signals"] == ["EMA(20)>EMA(50)"]
    assert by_id["grammar1"]["exit_kind"] == "time"
    assert by_id["grammar2"]["entry_signals"] == ["RSI(14)<30"]
    assert by_id["grammar2"]["filter_signals"] == ["EMA(20)>EMA(50)", "CMF(20)>0"]
    assert by_id["grammar2"]["exit_kind"] == "atr_trail"
    stopped = repo.leaderboard(
        "run1",
        ticker=None,
        family=None,
        min_trades=None,
        include_rejected=True,
        sort="robustness",
        order="desc",
        limit=10,
        offset=0,
        exit_kind="time",
        filter_count=1,
    )
    assert [item["strategy_id"] for item in stopped["items"]] == ["grammar1"]
    mirrors = repo.leaderboard(
        "run1",
        ticker=None,
        family=None,
        min_trades=None,
        include_rejected=True,
        sort="robustness",
        order="desc",
        limit=10,
        offset=0,
        exit_kind="mirror",
        filter_count=0,
    )
    assert [item["strategy_id"] for item in mirrors["items"]] == [legacy.id]
    trailed = repo.leaderboard(
        "run1",
        ticker=None,
        family=None,
        min_trades=None,
        include_rejected=True,
        sort="robustness",
        order="desc",
        limit=10,
        offset=0,
        exit_kind="atr_trail",
        filter_count=2,
    )
    assert [item["strategy_id"] for item in trailed["items"]] == ["grammar2"]


def test_leaderboard_sorts_signals_and_parameters_not_test_cagr(tmp_path):
    high = _strategy_row("High", "ZZZ signal", {"z_len": 1}, 90.0, 1)
    low = _strategy_row("Low", "AAA signal", {"a_len": 1}, 10.0, 2)
    repo = Repository(tmp_path)
    repo.save_sweep(
        {
            "runs": [
                {
                    "id": "run1",
                    "created_at": utc_now(),
                    "config_json": "{}",
                    "status": "completed",
                    "strategy_count": 2,
                    "ticker_count": 1,
                }
            ],
            "strategies": [high["strategy"], low["strategy"]],
            "results": [
                _result(str(high["id"]), "validation", 0.2, 1.5, 12),
                _result(str(high["id"]), "test", 0.01, 0.1, 4),
                _result(str(low["id"]), "validation", 0.05, 0.2, 12),
                _result(str(low["id"]), "test", 0.9, 3.0, 4),
            ],
            "robustness": [high["robustness"], low["robustness"]],
            "walk_forward": [],
            "reopt": [],
            "cross_ticker": [],
            "benchmarks": [],
        }
    )

    def names(sort: str, order: str = "desc") -> list[str]:
        board = repo.leaderboard(
            "run1",
            ticker=None,
            family=None,
            min_trades=None,
            include_rejected=False,
            sort=sort,
            order=order,
            limit=10,
            offset=0,
        )
        return [str(item["strategy"]) for item in board["items"]]

    assert names("robustness") == ["High", "Low"]
    assert names("test_cagr") == ["High", "Low"]
    assert names("not-a-column") == ["High", "Low"]
    assert names("signals", "asc") == ["Low", "High"]
    assert names("parameters", "asc") == ["Low", "High"]
    parameters = repo.leaderboard(
        "run1",
        ticker=None,
        family=None,
        min_trades=None,
        include_rejected=False,
        sort="parameters",
        order="asc",
        limit=10,
        offset=0,
    )
    assert [item["parameters"] for item in parameters["items"]] == ["a_len=1", "z_len=1"]


def _strategy_row(
    name: str, signals: str, parameters: dict[str, int | float], score: float, rank: int
) -> dict[str, Any]:
    entry = ConditionGroup(logic="AND", conditions=[cond("close", "gt", const(1))])
    exit_group = ConditionGroup(logic="OR", conditions=[cond("close", "lt", const(1))])
    strategy = Strategy(
        id=content_id(
            family="trend_momentum",
            entry=entry,
            exit_group=exit_group,
            parameters=parameters,
        ),
        name=name,
        family="trend_momentum",
        entry=entry,
        exit=exit_group,
        parameters=parameters,
        signals=signals,
    )
    return {
        "id": strategy.id,
        "strategy": {
            "id": strategy.id,
            "name": strategy.name,
            "family": strategy.family,
            "definition_json": strategy.model_dump_json(),
            "signals": strategy.signals,
        },
        "robustness": {
            "run_id": "run1",
            "strategy_id": strategy.id,
            "ticker": "IREN",
            "score": score,
            "sharpe_component": 0.4,
            "cagr_component": 0.2,
            "drawdown_component": 0.5,
            "profit_factor_component": 0.3,
            "trade_component": 0.4,
            "walk_forward_component": 0.5,
            "stability_component": 1.0,
            "degradation": 0.8,
            "flags_json": json.dumps([]),
            "rejected": False,
            "rank": rank,
        },
    }


def test_grouping_uses_robustness_and_counts_filtered_variants(tmp_path):
    assert "test_cagr" not in SORTS
    assert "test_cagr" not in GROUP_SORTS
    strong = _strategy_row("Twin", "A>1 | A<1", {"strong": 20}, 20.0, 1)
    weak = _strategy_row("Twin", "A>1 | A<1", {"weak": 10}, 10.0, 2)
    other = _strategy_row("Other", "B>1 | B<1", {"other": 10}, 5.0, 3)
    rejected = _strategy_row("Twin", "A>1 | A<1", {"rejected": 30}, 90.0, 4)
    rejected["robustness"]["rejected"] = True
    tie_low = _strategy_row("Tied", "C>1 | C<1", {"tie_low": 1}, 7.0, 5)
    tie_high = _strategy_row("Tied", "C>1 | C<1", {"tie_high": 2}, 7.0, 6)
    repo = Repository(tmp_path)
    repo.save_sweep(
        {
            "runs": [
                {
                    "id": "run1",
                    "created_at": utc_now(),
                    "config_json": "{}",
                    "status": "completed",
                    "strategy_count": 6,
                    "ticker_count": 1,
                }
            ],
            "strategies": [
                strong["strategy"],
                weak["strategy"],
                other["strategy"],
                rejected["strategy"],
                tie_low["strategy"],
                tie_high["strategy"],
            ],
            "results": [
                _result(str(strong["id"]), "validation", 0.2, 1.0, 12),
                _result(str(strong["id"]), "test", -0.5, 0.1, 4),
                _result(str(weak["id"]), "validation", 0.1, 0.2, 12),
                _result(str(weak["id"]), "test", 0.99, 3.0, 4),
                _result(str(other["id"]), "validation", 0.05, 2.0, 12),
                _result(str(other["id"]), "test", 0.01, 0.1, 4),
                _result(str(rejected["id"]), "validation", 0.4, 1.0, 12),
                _result(str(rejected["id"]), "test", 0.4, 1.0, 4),
                _result(str(tie_low["id"]), "validation", 0.1, 0.4, 12),
                _result(str(tie_high["id"]), "validation", 0.1, 0.4, 12),
            ],
            "robustness": [
                strong["robustness"],
                weak["robustness"],
                other["robustness"],
                rejected["robustness"],
                tie_low["robustness"],
                tie_high["robustness"],
            ],
            "walk_forward": [],
            "reopt": [],
            "cross_ticker": [],
            "benchmarks": [],
        }
    )

    def board(**extra: object) -> dict[str, object]:
        args: dict[str, object] = {
            "ticker": None,
            "family": None,
            "min_trades": None,
            "include_rejected": False,
            "sort": "robustness",
            "order": "desc",
            "limit": 10,
            "offset": 0,
            "group_variants": True,
        }
        args.update(extra)
        return repo.leaderboard("run1", **args)  # type: ignore[arg-type]

    grouped = board()
    assert grouped["total"] == 3
    by_name = {str(item["strategy"]): item for item in grouped["items"]}  # type: ignore[union-attr]
    assert by_name["Twin"]["strategy_id"] == strong["id"]
    assert by_name["Twin"]["variants"] == 2
    assert by_name["Twin"]["test_cagr"] == -0.5
    assert by_name["Tied"]["strategy_id"] == min(str(tie_low["id"]), str(tie_high["id"]))
    assert by_name["Tied"]["variants"] == 2

    with_rejected = board(include_rejected=True)
    twin = next(item for item in with_rejected["items"] if item["strategy"] == "Twin")  # type: ignore[union-attr]
    assert twin["strategy_id"] == rejected["id"]
    assert twin["variants"] == 3

    opened = board(group_variants=False, rule="Twin")
    assert opened["total"] == 2
    assert {item["strategy_id"] for item in opened["items"]} == {strong["id"], weak["id"]}  # type: ignore[union-attr]

    missing = board(rule="Missing")
    assert missing["total"] == 0
    assert missing["items"] == []

    first = board(limit=1, offset=0, sort="sharpe", order="desc")
    second = board(limit=1, offset=1, sort="sharpe", order="desc")
    third = board(limit=1, offset=2, sort="sharpe", order="desc")
    assert first["total"] == 3
    assert len(first["items"]) == len(second["items"]) == len(third["items"]) == 1  # type: ignore[arg-type]
    assert board(limit=1, offset=3)["items"] == []
    # Other has the highest validation Sharpe, so it leads even though its score is lowest.
    assert first["items"][0]["strategy"] == "Other"  # type: ignore[index]


def test_connect_drops_the_market_view(tmp_path):
    import duckdb

    from stocksweeper.storage.db import connect

    path = tmp_path / "results.duckdb"
    raw = duckdb.connect(str(path))
    raw.execute("CREATE VIEW market AS SELECT 1 AS n")
    raw.close()
    with connect(path) as connection:
        found = connection.execute("SELECT view_name FROM duckdb_views()").fetchall()
        names = [row[0] for row in found]
    assert "market" not in names


def _robust(strategy_id: str, score: float, rank: int) -> dict[str, object]:
    return {
        "run_id": "run1",
        "strategy_id": strategy_id,
        "ticker": "IREN",
        "score": score,
        "sharpe_component": 0.4,
        "cagr_component": 0.2,
        "drawdown_component": 0.5,
        "profit_factor_component": 0.3,
        "trade_component": 0.4,
        "walk_forward_component": 0.5,
        "stability_component": 1.0,
        "degradation": 0.8,
        "flags_json": json.dumps([]),
        "rejected": False,
        "rank": rank,
    }


def _result(
    strategy_id: str, segment: str, cagr: float, sharpe: float, trades: int
) -> dict[str, object]:
    return {
        "run_id": "run1",
        "strategy_id": strategy_id,
        "ticker": "IREN",
        "segment": segment,
        "cagr": cagr,
        "total_return": cagr,
        "sharpe": sharpe,
        "sortino": sharpe,
        "max_drawdown": -0.1,
        "calmar": 1.0,
        "win_rate": 0.5,
        "profit_factor": 1.2,
        "avg_trade": 0.01,
        "median_trade": 0.01,
        "n_trades": trades,
        "exposure": 0.3,
        "avg_holding_period": 5.0,
    }
