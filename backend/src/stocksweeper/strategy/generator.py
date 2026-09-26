"""One signal, up to two filters, and one exit from that signal's menu."""

from __future__ import annotations

import itertools

import numpy as np

from stocksweeper.indicators.names import IndicatorRequest, request_for
from stocksweeper.strategy.exits import exit_label, menu_for
from stocksweeper.strategy.model import ConditionGroup, ExitRule, Strategy, content_id
from stocksweeper.strategy.primitives import (
    PRIMITIVES,
    Primitive,
    combination_allowed,
)

FAMILY_ORDER: tuple[str, ...] = (
    "trend_following",
    "momentum_pullback",
    "momentum_continuation",
    "volume_pullback",
    "volume_continuation",
    "volatility_fade",
    "volatility_breakout",
)

_STRATEGY_FAMILY = {
    ("trend", "trend"): "trend_following",
    ("momentum", "pullback"): "momentum_pullback",
    ("momentum", "continuation"): "momentum_continuation",
    ("volume", "pullback"): "volume_pullback",
    ("volume", "continuation"): "volume_continuation",
    ("volatility", "fade"): "volatility_fade",
    ("volatility", "breakout"): "volatility_breakout",
}

_REGIME = frozenset({"trend", "momentum"})
_GATE = frozenset({"volume", "volatility"})
_EXIT_SUFFIX = {"time": ", time stop", "atr_trail": ", ATR trail"}

STRATA: tuple[str, ...] = tuple(
    f"{family}:{count}" for family in FAMILY_ORDER for count in (0, 1, 2)
)


def generate_strategies(max_strategies: int, seed: int) -> list[Strategy]:
    grouped: dict[str, list[Strategy]] = {key: [] for key in STRATA}
    for stratum, strategy in _catalog():
        grouped[stratum].append(strategy)
    return _stratified(grouped, max_strategies, seed)


def requests_for(strategies: list[Strategy]) -> list[IndicatorRequest]:
    found: dict[IndicatorRequest, None] = {}
    for strategy in strategies:
        if strategy.exit_rule.kind == "atr_trail":
            found.setdefault(
                IndicatorRequest.make("atr", {"length": strategy.exit_rule.atr_length}),
                None,
            )
        for group in (strategy.entry, strategy.exit):
            for condition in group.conditions:
                for indicator, params in (
                    (condition.indicator, condition.params),
                    (condition.rhs.indicator, condition.rhs.params),
                ):
                    if indicator is None:
                        continue
                    request = request_for(indicator, params)
                    if request is not None:
                        found.setdefault(request, None)
    return list(found)


def _catalog() -> list[tuple[str, Strategy]]:
    signals = [primitive for primitive in PRIMITIVES if primitive.role == "signal"]
    filters = [primitive for primitive in PRIMITIVES if primitive.role == "filter"]
    regime = [primitive for primitive in filters if primitive.family in _REGIME]
    gates = [primitive for primitive in filters if primitive.family in _GATE]
    order = {primitive.id: index for index, primitive in enumerate(PRIMITIVES)}
    found: list[tuple[str, Strategy]] = []
    seen: set[str] = set()
    for signal in signals:
        if signal.claim is None:
            continue
        family = _STRATEGY_FAMILY[(signal.family, signal.claim)]
        for chosen in _filter_sets(regime, gates):
            ordered = tuple(sorted(chosen, key=lambda primitive: order[primitive.id]))
            if not combination_allowed((signal, *ordered)):
                continue
            grids = [signal.grid, *[primitive.grid for primitive in ordered]]
            for params in itertools.product(*grids):
                signal_params = params[0]
                filter_params = params[1:]
                for rule in menu_for(signal.claim):
                    strategy = _strategy(
                        signal, signal_params, ordered, filter_params, rule, family
                    )
                    if strategy.id in seen:
                        continue
                    seen.add(strategy.id)
                    found.append((f"{family}:{len(ordered)}", strategy))
    return found


def _filter_sets(regime: list[Primitive], gates: list[Primitive]) -> list[tuple[Primitive, ...]]:
    sets: list[tuple[Primitive, ...]] = [()]
    sets.extend((primitive,) for primitive in (*regime, *gates))
    sets.extend(itertools.product(regime, gates))
    return sets


def _strategy(
    signal: Primitive,
    signal_params: dict[str, int | float],
    filters: tuple[Primitive, ...],
    filter_params: tuple[dict[str, int | float], ...],
    rule: ExitRule,
    family: str,
) -> Strategy:
    entry, exit_group, entry_label, mirror_label = signal.build(signal_params)
    entries = list(entry)
    entry_text = [entry_label]
    parameters: dict[str, int | float] = {
        f"{signal.id}_{key}": value for key, value in signal_params.items()
    }
    labels = [signal.label]
    for primitive, param in zip(filters, filter_params, strict=True):
        filter_entry, _filter_exit, filter_label, _filter_mirror = primitive.build(param)
        entries.extend(filter_entry)
        entry_text.append(filter_label)
        labels.append(primitive.label)
        for key, value in param.items():
            parameters[f"{primitive.id}_{key}"] = value
    if rule.kind == "mirror":
        exits = list(exit_group)
        exit_text = [mirror_label] if mirror_label else []
    else:
        exits = []
        exit_text = [exit_label(rule)]
        if rule.kind == "time":
            parameters["exit_time_bars"] = rule.bars
        else:
            parameters["exit_atr_mult"] = rule.atr_mult
    entry_group = ConditionGroup(logic="AND", conditions=entries)
    exit_conditions = ConditionGroup(logic="OR", conditions=exits)
    return Strategy(
        id=content_id(
            family=family,
            entry=entry_group,
            exit_group=exit_conditions,
            parameters=parameters,
            exit_rule=rule,
        ),
        name=" + ".join(labels) + _EXIT_SUFFIX.get(rule.kind, ""),
        family=family,
        entry=entry_group,
        exit=exit_conditions,
        parameters=parameters,
        signals=join_signals(entry_text, exit_text),
        exit_rule=rule,
        signal=signal.id,
        filters=[primitive.id for primitive in filters],
    )


def join_signals(entry: list[str], exits: list[str]) -> str:
    return " & ".join(entry) + " | " + " | ".join(exits)


def split_signals(signals: str) -> tuple[list[str], list[str]]:
    entry_text, separator, exit_text = signals.partition(" | ")
    entry = [part for part in entry_text.split(" & ") if part]
    if not separator:
        return entry, []
    return entry, [part for part in exit_text.split(" | ") if part]


def present_signals(
    signals: str, filters: object, exit_kind: object
) -> tuple[list[str], list[str], list[str], str]:
    """Split a stored signal string into the signal, its filters, and the exit."""
    entry, exits = split_signals(signals)
    names = [str(item) for item in filters] if isinstance(filters, list) else []
    kind = str(exit_kind) if exit_kind in {"mirror", "atr_trail", "time"} else "mirror"
    if names and len(names) < len(entry):
        return entry[: -len(names)], entry[-len(names) :], exits, kind
    return entry, [], exits, kind


def _stratified(
    grouped: dict[str, list[Strategy]], max_strategies: int, seed: int
) -> list[Strategy]:
    keys = [name for name in STRATA if grouped.get(name)]
    total = sum(len(grouped[key]) for key in keys)
    if total <= max_strategies:
        return [combo for key in keys for combo in grouped[key]]
    rng = np.random.default_rng(seed)
    allocation = {key: min(len(grouped[key]), max_strategies // len(keys)) for key in keys}
    remaining = max_strategies - sum(allocation.values())
    order = sorted(keys, key=lambda key: len(grouped[key]) - allocation[key], reverse=True)
    while remaining > 0:
        progressed = False
        for key in order:
            if len(grouped[key]) - allocation[key] > 0 and remaining > 0:
                allocation[key] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    chosen: list[Strategy] = []
    for key in keys:
        items = grouped[key]
        count = allocation[key]
        if count >= len(items):
            chosen.extend(items)
            continue
        indexes = np.sort(rng.choice(len(items), size=count, replace=False))
        chosen.extend(items[int(index)] for index in indexes)
    return chosen
