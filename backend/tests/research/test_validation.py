import inspect

import numpy as np

from stocksweeper.backtest.metrics import Metrics
from stocksweeper.config import GateSettings, RobustnessWeights, ValidationSettings
from stocksweeper.strategy.model import ConditionGroup, Strategy
from stocksweeper.strategy.primitives import BY_ID
from stocksweeper.validation.robustness import score_strategy
from stocksweeper.validation.sensitivity import stability_scores
from stocksweeper.validation.splits import split_segments
from stocksweeper.validation.walkforward import consistency, fold_windows


def _metrics(**overrides: float | int | None) -> Metrics:
    base: dict[str, float | int | None] = {
        "cagr": 0.1,
        "total_return": 0.2,
        "sharpe": 1.0,
        "sortino": 1.2,
        "max_drawdown": -0.2,
        "calmar": 0.5,
        "win_rate": 0.55,
        "profit_factor": 1.4,
        "avg_trade": 0.01,
        "median_trade": 0.01,
        "n_trades": 30,
        "exposure": 0.4,
        "avg_holding_period": 8,
    }
    base.update(overrides)
    return Metrics(**base)  # type: ignore[arg-type]


def test_split_boundaries_and_limited_history():
    settings = ValidationSettings()
    limited, bounds = split_segments(1000, settings)
    assert not limited
    assert bounds == {"train": (0, 600), "validation": (600, 800), "test": (800, 1000)}
    limited, short = split_segments(100, settings)
    assert limited
    assert short["train"] == (0, 50)
    assert short["validation"] == (50, 75)
    assert short["test"] == (75, 100)


def test_walk_forward_stays_inside_the_non_test_region():
    windows = fold_windows(80, 5)
    assert len(windows) == 5
    assert windows[0] == (0, 13, 13, 26)
    assert windows[-1][3] <= 80
    ordered = all(
        oos_end > oos_start >= is_end > is_start or is_start == 0
        for is_start, is_end, oos_start, oos_end in windows
    )
    assert ordered


def test_score_ignores_the_test_segment_and_rises_with_validation_sharpe():
    assert "test" not in inspect.signature(score_strategy).parameters
    weights = RobustnessWeights()
    gates = GateSettings(
        min_trades=5,
        min_val_trades=1,
        max_drawdown=0.9,
        min_degradation=0.0,
        min_stability=0.0,
    )
    weak = score_strategy(
        _metrics(sharpe=1.0),
        _metrics(sharpe=0.4),
        consistency=0.5,
        stability=1.0,
        weights=weights,
        gates=gates,
    )
    strong = score_strategy(
        _metrics(sharpe=1.0),
        _metrics(sharpe=1.2),
        consistency=0.5,
        stability=1.0,
        weights=weights,
        gates=gates,
    )
    assert strong.score > weak.score


def test_gates_flag_fragile_strategies():
    weights = RobustnessWeights()
    gates = GateSettings()
    scored = score_strategy(
        _metrics(sharpe=2.0, n_trades=4),
        _metrics(sharpe=0.2, n_trades=1, max_drawdown=-0.8),
        consistency=0.2,
        stability=0.1,
        weights=weights,
        gates=gates,
    )
    assert scored.rejected
    assert "insufficient_trades" in scored.flags
    assert "extreme_drawdown" in scored.flags
    assert "severe_degradation" in scored.flags
    assert "unstable_parameters" in scored.flags


def test_consistency_rewards_positive_folds():
    assert consistency(np.array([1.0, 1.0, 1.0])) > consistency(np.array([-1.0, -1.0, -1.0]))


def test_missing_neighbours_are_not_punished():
    built = BY_ID["ema_stack"].build({"fast": 10, "slow": 50})
    strategy = Strategy(
        id="abc",
        name="ema",
        family="trend_following",
        entry=ConditionGroup(logic="AND", conditions=built[0]),
        exit=ConditionGroup(logic="OR", conditions=built[1]),
        parameters={"ema_stack_fast": 10, "ema_stack_slow": 50},
        signals="x",
    )
    scores = stability_scores([strategy], {"abc": 1.0})
    assert scores["abc"].score == 0.5
    assert scores["abc"].neighbours == 0
    neutral = score_strategy(
        _metrics(),
        _metrics(),
        consistency=1.0,
        stability=scores["abc"].score,
        weights=RobustnessWeights(),
        gates=GateSettings(),
        neighbours=0,
    )
    assert "no_neighbours" in neutral.flags
    assert neutral.rejected is False


def test_negative_sharpe_uses_the_neighbour_ratio():
    own = _ema("own", 10)
    neighbour = _ema("neighbour", 20)
    matched = stability_scores([own, neighbour], {"own": -1.0, "neighbour": -1.0})
    assert matched["own"].neighbours == 1
    assert matched["own"].score == 1.0
    kept = score_strategy(
        _metrics(sharpe=-1.0),
        _metrics(sharpe=-1.0),
        consistency=1.0,
        stability=matched["own"].score,
        weights=RobustnessWeights(),
        gates=GateSettings(),
        neighbours=matched["own"].neighbours,
    )
    assert "unstable_parameters" not in kept.flags
    assert kept.rejected is False
    closer = stability_scores([own, neighbour], {"own": -1.0, "neighbour": -0.25})
    assert closer["own"].score == 0.25
    worse = stability_scores([own, neighbour], {"own": -0.5, "neighbour": -2.0})
    assert worse["own"].score == 1.0
    zero = stability_scores([own, neighbour], {"own": 0.0, "neighbour": -1.0})
    assert zero["own"].score == 0.0
    missing = stability_scores([own, neighbour], {"own": None, "neighbour": 1.0})
    assert missing["own"].score == 0.0
    undefined = stability_scores([own, neighbour], {"own": float("nan"), "neighbour": 1.0})
    assert undefined["own"].score == 0.0


def _ema(strategy_id: str, fast: int) -> Strategy:
    built = BY_ID["ema_stack"].build({"fast": fast, "slow": 50})
    return Strategy(
        id=strategy_id,
        name="ema",
        family="trend_following",
        entry=ConditionGroup(logic="AND", conditions=built[0]),
        exit=ConditionGroup(logic="OR", conditions=built[1]),
        parameters={"ema_stack_fast": fast, "ema_stack_slow": 50},
        signals="x",
    )
