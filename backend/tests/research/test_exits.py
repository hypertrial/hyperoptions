import numpy as np
import pandas as pd
import pytest

from stocksweeper.backtest.engine import simulate
from stocksweeper.config import load_settings
from stocksweeper.indicators.names import IndicatorRequest
from stocksweeper.strategy.compiler import compile_strategy
from stocksweeper.strategy.exits import apply_exit, exit_label, menu_for
from stocksweeper.strategy.generator import requests_for
from stocksweeper.strategy.model import (
    ConditionGroup,
    ExitRule,
    Strategy,
    cond,
    const,
    content_id,
)


def _flat_settings():
    settings = load_settings()
    return settings.model_copy(
        update={"backtest": settings.backtest.model_copy(update={"fees": 0.0, "slippage": 0.0})}
    )


def _strategy(rule: ExitRule, entry: ConditionGroup, exit_group: ConditionGroup) -> Strategy:
    return Strategy(
        id=content_id(
            family="test",
            entry=entry,
            exit_group=exit_group,
            parameters={},
            exit_rule=rule,
        ),
        name="test",
        family="test",
        entry=entry,
        exit=exit_group,
        parameters={},
        signals="test",
        exit_rule=rule,
    )


def _trades(entries, exits, close, rule, atr=None):
    count = len(close)
    widths = np.ones(count) if atr is None else atr
    adjusted_entries, adjusted_exits = apply_exit(entries, exits, close, widths, rule)
    index = pd.bdate_range("2020-01-01", periods=count)
    opened = np.asarray(close, dtype=float)
    _, trades = simulate(
        index,
        opened,
        opened,
        adjusted_entries.reshape(-1, 1),
        adjusted_exits.reshape(-1, 1),
        _flat_settings(),
    )
    return trades[0], adjusted_entries, adjusted_exits


def _specified(entries, exits, close, atr, kind, bars, mult):
    """Fill indexes implied by the exit rules, independent of the state machine."""
    count = len(entries)
    in_position = False
    signal_bar = 0
    peak = -np.inf
    trades: list[tuple[int, int, bool]] = []
    for bar in range(count):
        if in_position:
            fill = signal_bar + 1
            if bar >= fill and np.isfinite(close[bar]) and close[bar] > peak:
                peak = close[bar]
            stop = (kind == "time" and bar - signal_bar == bars) or (
                kind == "atr"
                and bar >= fill
                and np.isfinite(close[bar])
                and close[bar] < peak - mult * atr[signal_bar]
            )
            if exits[bar] or stop:
                entry_idx = signal_bar + 1
                if bar + 1 < count:
                    trades.append((entry_idx, bar + 1, True))
                else:
                    trades.append((entry_idx, -1, False))
                in_position = False
            continue
        if entries[bar] and not exits[bar] and bar + 1 < count:
            if kind == "atr" and not np.isfinite(atr[bar]):
                continue
            in_position = True
            signal_bar = bar
            peak = -np.inf
    if in_position:
        trades.append((signal_bar + 1, -1, False))
    return trades


def test_time_stop_holds_for_exactly_n_bars():
    close = np.linspace(10.0, 20.0, 25)
    entries = np.zeros(25, dtype=bool)
    exits = np.zeros(25, dtype=bool)
    entries[0] = True
    trades, _, _ = _trades(entries, exits, close, ExitRule(kind="time", bars=10))
    assert len(trades) == 1
    assert trades[0].closed is True
    assert trades[0].exit_idx - trades[0].entry_idx == 10
    assert trades[0].entry_idx == 1
    assert trades[0].exit_idx == 11


def test_time_stop_hold_is_n_when_entry_is_true_on_the_exit_bar():
    """An entry on the stop bar must not cancel the exit or shorten the hold."""
    close = np.linspace(10.0, 35.0, 25)
    entries = np.zeros(25, dtype=bool)
    entries[0] = True
    entries[10] = True
    trades, entry_signals, exit_signals = _trades(
        entries, np.zeros(25, dtype=bool), close, ExitRule(kind="time", bars=10)
    )
    assert exit_signals[10]
    assert not entry_signals[10]
    closed = [trade for trade in trades if trade.closed]
    assert len(closed) == 1
    assert closed[0].exit_idx - closed[0].entry_idx == 10
    assert closed[0].entry_idx == 1
    assert closed[0].exit_idx == 11


def test_one_bar_time_stop_exits_on_the_fill_bar_close():
    close = np.arange(10.0, 16.0)
    entries = np.zeros(6, dtype=bool)
    entries[0] = True
    trades, signals, _ = _trades(
        entries, np.zeros(6, dtype=bool), close, ExitRule(kind="time", bars=1)
    )
    assert signals.tolist() == [True, False, False, False, False, False]
    assert trades[0].entry_idx == 1
    assert trades[0].exit_idx == 2


def test_atr_trail_ratchets_and_signals_on_the_breach_bar():
    close = np.array([10.0, 10.0, 11.0, 13.0, 12.0, 10.0, 9.0, 9.0])
    atr = np.ones(8)
    entries = np.zeros(8, dtype=bool)
    entries[0] = True
    trades, _, exit_signals = _trades(
        entries,
        np.zeros(8, dtype=bool),
        close,
        ExitRule(kind="atr_trail", atr_length=14, atr_mult=2.0),
        atr,
    )
    assert exit_signals.tolist() == [False, False, False, False, False, True, False, False]
    assert trades[0].closed is True
    assert trades[0].entry_idx == 1
    assert trades[0].exit_idx == 6


def test_atr_trail_peak_includes_fill_close_and_skips_new_highs():
    # Gap down on the fill bar: peak is that fill close, not the entry-signal close.
    gap = np.array([100.0, 50.0, 50.0, 50.0, 19.0, 19.0])
    gap_entries = np.zeros(6, dtype=bool)
    gap_entries[0] = True
    gap_trades, _, gap_exits = _trades(
        gap_entries,
        np.zeros(6, dtype=bool),
        gap,
        ExitRule(kind="atr_trail", atr_mult=3.0),
        np.full(6, 10.0),
    )
    assert gap_exits.tolist() == [False, False, False, False, True, False]
    assert gap_trades[0].closed is True
    assert gap_trades[0].entry_idx == 1
    assert gap_trades[0].exit_idx == 5

    # Rising closes only ratchet the peak; none of those bars are stops.
    rising = np.array([10.0, 10.0, 12.0, 14.0, 16.0, 15.5, 12.0, 12.0])
    rising_entries = np.zeros(8, dtype=bool)
    rising_entries[0] = True
    rising_trades, _, rising_exits = _trades(
        rising_entries,
        np.zeros(8, dtype=bool),
        rising,
        ExitRule(kind="atr_trail", atr_mult=3.0),
        np.ones(8),
    )
    assert rising_exits.tolist() == [False, False, False, False, False, False, True, False]
    assert rising_trades[0].exit_idx - rising_trades[0].entry_idx == 6


def test_atr_trail_does_not_enter_while_atr_is_missing():
    close = np.arange(10.0, 15.0)
    atr = np.array([np.nan, 1.0, 1.0, 1.0, 1.0])
    entries = np.zeros(5, dtype=bool)
    entries[0] = True
    trades, entry_signals, _ = _trades(
        entries,
        np.zeros(5, dtype=bool),
        close,
        ExitRule(kind="atr_trail", atr_mult=3.0),
        atr,
    )
    assert trades == []
    assert not entry_signals.any()


def test_no_position_opens_on_the_last_bar():
    close = np.arange(5.0, 10.0)
    entries = np.zeros(5, dtype=bool)
    entries[-1] = True
    trades, entry_signals, _ = _trades(
        entries, np.zeros(5, dtype=bool), close, ExitRule(kind="time", bars=2)
    )
    assert trades == []
    assert entry_signals[-1] is np.False_


def test_entries_are_ignored_until_the_stop_has_fired():
    count = 12
    close = np.linspace(10.0, 21.0, count)
    entries = np.ones(count, dtype=bool)
    trades, _, _ = _trades(
        entries, np.zeros(count, dtype=bool), close, ExitRule(kind="time", bars=4)
    )
    closed = [(trade.entry_idx, trade.exit_idx) for trade in trades if trade.closed]
    assert closed == [(1, 5), (6, 10)]
    assert trades[-1].closed is False
    assert trades[-1].entry_idx == 11


def test_stop_trades_match_the_fill_specification_on_seeded_noise():
    rng = np.random.default_rng(7)
    count = 180
    close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, count))
    atr = rng.uniform(0.5, 2.5, count)
    atr[::37] = np.nan
    entries = rng.random(count) < 0.08
    exits = rng.random(count) < 0.05
    rule = ExitRule(kind="atr_trail", atr_mult=2.5)
    trades, _, _ = _trades(entries, exits, close, rule, atr)
    expected = _specified(entries, exits, close, atr, "atr", rule.bars, rule.atr_mult)
    assert [(trade.entry_idx, trade.exit_idx, trade.closed) for trade in trades] == expected

    time_rule = ExitRule(kind="time", bars=6)
    time_trades, _, _ = _trades(entries, np.zeros(count, dtype=bool), close, time_rule, atr)
    time_expected = _specified(entries, np.zeros(count, dtype=bool), close, atr, "time", 6, 3.0)
    observed = [(trade.entry_idx, trade.exit_idx, trade.closed) for trade in time_trades]
    assert observed == time_expected


def test_mirror_compilation_and_content_id_are_unchanged():
    frame = pd.DataFrame(
        {
            "close": [1.0, 1.0, 3.0, 4.0, 0.0],
            "ema_10": [1.0, 2.0, 2.0, 2.0, 2.0],
            "ema_20": [2.0, 2.0, 1.0, 1.0, 3.0],
        }
    )
    entry = ConditionGroup(
        logic="AND",
        conditions=[cond("ema", "gt", const(0), {"length": 10})],
    )
    exit_group = ConditionGroup(logic="OR", conditions=[])
    bare = content_id(family="test", entry=entry, exit_group=exit_group, parameters={})
    mirrored = content_id(
        family="test", entry=entry, exit_group=exit_group, parameters={}, exit_rule=ExitRule()
    )
    stopped = content_id(
        family="test",
        entry=entry,
        exit_group=exit_group,
        parameters={},
        exit_rule=ExitRule(kind="time", bars=10),
    )
    assert bare == mirrored
    assert bare != stopped
    strategy = _strategy(ExitRule(), entry, exit_group)
    entries, exits = compile_strategy(frame, strategy)
    assert entries.tolist() == [True, True, True, True, True]
    assert exits.tolist() == [False, False, False, False, False]

    legacy = Strategy.model_validate(
        {
            "id": bare,
            "name": "legacy",
            "family": "test",
            "entry": entry.model_dump(mode="json"),
            "exit": exit_group.model_dump(mode="json"),
            "parameters": {},
            "signals": "ema | ",
        }
    )
    assert legacy.id == bare
    assert legacy.exit_rule.kind == "mirror"
    assert legacy.signal is None
    assert legacy.filters == []
    legacy_entries, legacy_exits = compile_strategy(frame, legacy)
    assert legacy_entries.tolist() == entries.tolist()
    assert legacy_exits.tolist() == exits.tolist()


def test_compiled_atr_trail_reads_the_hidden_atr_column():
    close = np.array([10.0, 10.0, 11.0, 13.0, 12.0, 10.0, 9.0, 9.0])
    frame = pd.DataFrame({"close": close, "atr_14": np.ones(8)})
    entry = ConditionGroup(
        logic="AND",
        conditions=[cond("close", "gt", const(0))],
    )
    rule = ExitRule(kind="atr_trail", atr_length=14, atr_mult=2.0)
    strategy = _strategy(rule, entry, ConditionGroup(logic="OR", conditions=[]))
    compiled_entries, compiled_exits = compile_strategy(frame, strategy)
    assert compiled_entries[0] and compiled_exits[5]
    with pytest.raises(KeyError, match="atr_14"):
        compile_strategy(frame.drop(columns=["atr_14"]), strategy)


def test_atr_trail_is_requested_without_an_atr_condition():
    entry = ConditionGroup(logic="AND", conditions=[cond("close", "gt", const(0))])
    empty = ConditionGroup(logic="OR", conditions=[])
    trail = _strategy(ExitRule(kind="atr_trail", atr_length=14), entry, empty)
    mirror = _strategy(ExitRule(), entry, empty)
    assert IndicatorRequest.make("atr", {"length": 14}) in requests_for([trail])
    assert requests_for([mirror]) == []


def test_exit_menu_follows_the_claim():
    assert [rule.kind for rule in menu_for("pullback")] == ["mirror", "time"]
    assert [rule.kind for rule in menu_for("fade")] == ["mirror", "time"]
    assert [rule.kind for rule in menu_for("trend")] == ["mirror", "atr_trail"]
    assert [rule.kind for rule in menu_for("continuation")] == ["mirror", "atr_trail"]
    assert [rule.kind for rule in menu_for("breakout")] == ["mirror", "atr_trail"]
    assert exit_label(menu_for("pullback")[1]) == "Time stop 10 bars"
    assert exit_label(menu_for("trend")[1]) == "ATR(14) trail 3x"
    with pytest.raises(ValueError, match="unknown claim"):
        menu_for("regime")
