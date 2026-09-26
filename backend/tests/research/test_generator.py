from collections import Counter

from stocksweeper.strategy.exits import menu_for
from stocksweeper.strategy.generator import (
    FAMILY_ORDER,
    STRATA,
    generate_strategies,
    join_signals,
    split_signals,
)
from stocksweeper.strategy.primitives import BY_ID, PRIMITIVES, combination_allowed, parameter_axes
from stocksweeper.validation.sensitivity import stability_scores

# Pinned so a catalog edit has to say which count it meant to change.
EXPECTED_COUNTS = {
    ("momentum_continuation", 0): 14,
    ("momentum_continuation", 1): 208,
    ("momentum_continuation", 2): 750,
    ("momentum_pullback", 0): 16,
    ("momentum_pullback", 1): 224,
    ("momentum_pullback", 2): 768,
    ("trend_following", 0): 30,
    ("trend_following", 1): 464,
    ("trend_following", 2): 1718,
    ("volatility_breakout", 0): 8,
    ("volatility_breakout", 1): 128,
    ("volatility_breakout", 2): 464,
    ("volatility_fade", 0): 4,
    ("volatility_fade", 1): 64,
    ("volatility_fade", 2): 232,
    ("volume_continuation", 0): 4,
    ("volume_continuation", 1): 64,
    ("volume_continuation", 2): 232,
    ("volume_pullback", 0): 8,
    ("volume_pullback", 1): 112,
    ("volume_pullback", 2): 384,
}

_MEAN_REVERSION = frozenset({"momentum_pullback", "volume_pullback", "volatility_fade"})
_REGIME = frozenset({"trend", "momentum"})


def test_generator_is_deterministic_and_capped():
    first = generate_strategies(40, seed=7)
    second = generate_strategies(40, seed=7)
    assert [item.id for item in first] == [item.id for item in second]
    assert len(first) == 40
    assert len({item.id for item in first}) == 40
    assert {item.family for item in first} <= set(FAMILY_ORDER)
    for strategy in first:
        assert strategy.entry.logic == "AND"
        assert strategy.exit.logic == "OR"
        assert strategy.entry.conditions
        assert strategy.signal
        if strategy.exit_rule.kind == "mirror":
            assert strategy.exit.conditions
        else:
            assert strategy.exit.conditions == []


def test_signal_lists_round_trip_every_generated_strategy():
    strategies = generate_strategies(80, seed=7)
    assert strategies
    for strategy in strategies:
        entry, exits = split_signals(strategy.signals)
        assert join_signals(entry, exits) == strategy.signals
        assert entry
        assert exits
        assert entry[0]


def test_full_catalog_is_under_the_cap_and_pinned():
    strategies = generate_strategies(20_000, seed=7)
    assert len(strategies) == sum(EXPECTED_COUNTS.values())
    assert len(strategies) < 20_000
    assert len({item.id for item in strategies}) == len(strategies)
    counts = Counter((item.family, len(item.filters)) for item in strategies)
    assert dict(counts) == EXPECTED_COUNTS


def test_grammar_rules():
    strategies = generate_strategies(20_000, seed=7)
    regime_ids = {
        item.id for item in PRIMITIVES if item.role == "filter" and item.family in _REGIME
    }
    gate_ids = {
        item.id for item in PRIMITIVES if item.role == "filter" and item.family not in _REGIME
    }
    for strategy in strategies:
        assert strategy.signal
        assert strategy.signal not in strategy.filters
        assert 0 <= len(strategy.filters) <= 2
        if len(strategy.filters) == 2:
            assert len(set(strategy.filters) & regime_ids) == 1
            assert len(set(strategy.filters) & gate_ids) == 1
        chosen = tuple(BY_ID[item] for item in (strategy.signal, *strategy.filters))
        assert combination_allowed(chosen)
        assert all(item.role == "filter" for item in chosen[1:])
        claim = chosen[0].claim or ""
        assert strategy.exit_rule.kind in {rule.kind for rule in menu_for(claim)}
        if strategy.family in _MEAN_REVERSION:
            assert strategy.exit_rule.kind in {"mirror", "time"}
        else:
            assert strategy.exit_rule.kind in {"mirror", "atr_trail"}
        assert "adx_min" not in strategy.signals


def test_a_small_cap_still_covers_every_stratum():
    strategies = generate_strategies(len(STRATA), seed=7)
    found = {f"{item.family}:{len(item.filters)}" for item in strategies}
    assert found == set(STRATA)


def test_stop_twins_are_not_parameter_neighbours():
    strategies = [
        item
        for item in generate_strategies(20_000, seed=7)
        if item.signal == "rsi_pullback" and not item.filters
    ]
    assert {item.exit_rule.kind for item in strategies} == {"mirror", "time"}
    scores = stability_scores(strategies, {item.id: 1.0 for item in strategies})
    mirror = next(item for item in strategies if item.exit_rule.kind == "mirror")
    stopped = next(item for item in strategies if item.exit_rule.kind == "time")
    assert scores[mirror.id].neighbours > 0
    assert scores[stopped.id].neighbours > 0
    # Neighbours share the exit kind: flipping only the exit key finds no twin.
    assert "exit_time_bars" in stopped.parameters
    assert "exit_time_bars" not in mirror.parameters
    by_id = {item.id: item for item in strategies}
    mirror_neighbours = _neighbour_ids(mirror, by_id)
    stopped_neighbours = _neighbour_ids(stopped, by_id)
    assert stopped.id not in mirror_neighbours
    assert mirror.id not in stopped_neighbours
    assert {by_id[item].exit_rule.kind for item in mirror_neighbours} == {"mirror"}
    assert {by_id[item].exit_rule.kind for item in stopped_neighbours} == {"time"}


def _neighbour_ids(strategy, by_id: dict) -> set[str]:
    axes = parameter_axes()
    index = {
        (item.family, tuple(sorted(item.parameters.items()))): item.id
        for item in by_id.values()
    }
    found: set[str] = set()
    for key, value in strategy.parameters.items():
        axis = axes.get(key, [])
        if value not in axis:
            continue
        position = axis.index(value)
        for step in (position - 1, position + 1):
            if step < 0 or step >= len(axis):
                continue
            altered = dict(strategy.parameters)
            altered[key] = axis[step]
            match = index.get((strategy.family, tuple(sorted(altered.items()))))
            if match is not None and match != strategy.id:
                found.add(match)
    return found


def test_primitive_labels_are_unique():
    labels = [primitive.label for primitive in PRIMITIVES]
    assert len(labels) == len(set(labels))
    assert len(labels) == len(BY_ID)
    assert {primitive.role for primitive in PRIMITIVES} == {"signal", "filter"}
    series = {primitive.series for primitive in PRIMITIVES}
    assert all(
        sum(item.role == "signal" for item in PRIMITIVES if item.series == name) >= 1
        or name in {"adx", "atr"}
        for name in series
    )


def test_redundancy_guards():
    assert not combination_allowed((BY_ID["ema_stack"], BY_ID["sma_stack"]))
    assert not combination_allowed((BY_ID["ema_stack"], BY_ID["sma_filter"]))
    assert not combination_allowed((BY_ID["bb_pullback"], BY_ID["kc_breakout"]))
    assert not combination_allowed((BY_ID["bb_breakout"], BY_ID["bb_filter"]))
    assert not combination_allowed((BY_ID["rsi_pullback"], BY_ID["mfi_pullback"]))
    assert not combination_allowed((BY_ID["rsi_pullback"], BY_ID["rsi_filter"]))
    assert not combination_allowed((BY_ID["rsi_continuation"], BY_ID["rsi_pullback"]))
    assert combination_allowed((BY_ID["ema_stack"], BY_ID["rsi_filter"], BY_ID["cmf_filter"]))
    assert set(BY_ID["atr_regime"].grid[0].values()) == {0.08, 0.12} or {
        point["max_pct"] for point in BY_ID["atr_regime"].grid
    } == {0.08, 0.12}
