import numpy as np
import pandas as pd

from stocksweeper.strategy.compiler import compile_strategy
from stocksweeper.strategy.model import ConditionGroup, Strategy, cond, const, content_id, ref


def _strategy(entry: ConditionGroup, exit_group: ConditionGroup) -> Strategy:
    return Strategy(
        id=content_id(family="test", entry=entry, exit_group=exit_group, parameters={}),
        name="test",
        family="test",
        entry=entry,
        exit=exit_group,
        parameters={},
        signals="test",
    )


def test_and_or_and_crosses():
    frame = pd.DataFrame(
        {
            "close": [1.0, 1.0, 3.0, 4.0, 0.0],
            "ema_10": [1.0, 2.0, 2.0, 2.0, 2.0],
            "ema_20": [2.0, 2.0, 1.0, 1.0, 3.0],
            "rsi_14": [50.0, 30.0, 30.0, 80.0, 80.0],
        }
    )
    entry = ConditionGroup(
        logic="AND",
        conditions=[
            cond("ema", "gt", ref("ema", {"length": 20}), {"length": 10}),
            cond("rsi", "lt", const(40), {"length": 14}),
        ],
    )
    exit_group = ConditionGroup(
        logic="OR",
        conditions=[
            cond("ema", "lt", ref("ema", {"length": 20}), {"length": 10}),
            cond("rsi", "gt", const(70), {"length": 14}),
        ],
    )
    entries, exits = compile_strategy(frame, _strategy(entry, exit_group))
    assert entries.tolist() == [False, False, True, False, False]
    assert exits.tolist() == [True, False, False, True, True]

    cross = ConditionGroup(
        logic="AND",
        conditions=[cond("close", "crosses_above", const(2))],
    )
    empty_exit = ConditionGroup(logic="OR", conditions=[])
    crossed, _ = compile_strategy(frame, _strategy(cross, empty_exit))
    assert crossed.tolist() == [False, False, True, False, False]
    assert not np.isnan(frame["close"]).any()
