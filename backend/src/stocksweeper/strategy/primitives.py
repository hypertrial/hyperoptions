"""Signal and filter primitives. A recipe takes one signal and up to two filters."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass

from stocksweeper.strategy.model import Condition, cond, const, ref

Params = dict[str, int | float]
Built = tuple[list[Condition], list[Condition], str, str]


def _grid(
    axes: dict[str, tuple[int | float, ...]],
    predicate: Callable[[Params], bool] | None = None,
) -> tuple[Params, ...]:
    keys = list(axes)
    combos: list[Params] = []
    for values in itertools.product(*(axes[key] for key in keys)):
        params = dict(zip(keys, values, strict=True))
        if predicate is None or predicate(params):
            combos.append(params)
    return tuple(combos)


def _ema(params: Params) -> Built:
    fast, slow = int(params["fast"]), int(params["slow"])
    entry = [cond("ema", "gt", ref("ema", {"length": slow}), {"length": fast})]
    exit_group = [cond("ema", "lt", ref("ema", {"length": slow}), {"length": fast})]
    return entry, exit_group, f"EMA({fast})>EMA({slow})", f"EMA({fast})<EMA({slow})"


def _sma(params: Params) -> Built:
    fast, slow = int(params["fast"]), int(params["slow"])
    entry = [cond("sma", "gt", ref("sma", {"length": slow}), {"length": fast})]
    exit_group = [cond("sma", "lt", ref("sma", {"length": slow}), {"length": fast})]
    return entry, exit_group, f"SMA({fast})>SMA({slow})", f"SMA({fast})<SMA({slow})"


def _supertrend(params: Params) -> Built:
    spec = {"length": int(params["length"]), "multiplier": float(params["multiplier"])}
    label = f"Supertrend({spec['length']},{spec['multiplier']:.1f})"
    return (
        [cond("supertrend_dir", "gt", const(0), spec)],
        [cond("supertrend_dir", "lt", const(0), spec)],
        f"{label} up",
        f"{label} down",
    )


def _psar(params: Params) -> Built:
    spec = {"af": float(params["af"]), "max_af": float(params["max_af"])}
    return (
        [cond("psar_dir", "gt", const(0), spec)],
        [cond("psar_dir", "lt", const(0), spec)],
        "PSAR up",
        "PSAR down",
    )


def _aroon(params: Params) -> Built:
    length = int(params["length"])
    spec = {"length": length}
    return (
        [
            cond("aroon_up", "gt", ref("aroon_down", spec), spec),
            cond("aroon_up", "gt", const(70), spec),
        ],
        [cond("aroon_down", "gt", ref("aroon_up", spec), spec)],
        f"Aroon({length}) up",
        f"Aroon({length}) down",
    )


def _rsi(params: Params) -> Built:
    length = int(params["length"])
    entry_max = float(params["entry_max"])
    exit_min = float(params["exit_min"])
    spec = {"length": length}
    return (
        [cond("rsi", "lt", const(entry_max), spec)],
        [cond("rsi", "gt", const(exit_min), spec)],
        f"RSI({length})<{entry_max:g}",
        f"RSI({length})>{exit_min:g}",
    )


def _rsi_continuation(params: Params) -> Built:
    length = int(params["length"])
    entry_min = float(params["entry_min"])
    exit_max = float(params["exit_max"])
    spec = {"length": length}
    return (
        [cond("rsi", "gt", const(entry_min), spec)],
        [cond("rsi", "lt", const(exit_max), spec)],
        f"RSI({length})>{entry_min:g}",
        f"RSI({length})<{exit_max:g}",
    )


def _stoch(params: Params) -> Built:
    spec = {"k": int(params["k"]), "d": int(params["d"]), "smooth": int(params["smooth"])}
    entry_max = float(params["entry_max"])
    exit_min = float(params["exit_min"])
    return (
        [cond("stoch_k", "lt", const(entry_max), spec)],
        [cond("stoch_k", "gt", const(exit_min), spec)],
        f"Stoch({spec['k']})<{entry_max:g}",
        f"Stoch({spec['k']})>{exit_min:g}",
    )


def _macd(params: Params) -> Built:
    spec = {
        "fast": int(params["fast"]),
        "slow": int(params["slow"]),
        "signal": int(params["signal"]),
    }
    return (
        [cond("macd_hist", "gt", const(0), spec)],
        [cond("macd_hist", "lt", const(0), spec)],
        f"MACD({spec['fast']},{spec['slow']},{spec['signal']})>0",
        f"MACD({spec['fast']},{spec['slow']},{spec['signal']})<0",
    )


def _roc(params: Params) -> Built:
    length = int(params["length"])
    spec = {"length": length}
    return (
        [cond("roc", "gt", const(0), spec)],
        [cond("roc", "lt", const(0), spec)],
        f"ROC({length})>0",
        f"ROC({length})<0",
    )


def _cmf(params: Params) -> Built:
    length = int(params["length"])
    spec = {"length": length}
    return (
        [cond("cmf", "gt", const(0), spec)],
        [cond("cmf", "lt", const(0), spec)],
        f"CMF({length})>0",
        f"CMF({length})<0",
    )


def _mfi(params: Params) -> Built:
    length = int(params["length"])
    entry_max = float(params["entry_max"])
    exit_min = float(params["exit_min"])
    spec = {"length": length}
    return (
        [cond("mfi", "lt", const(entry_max), spec)],
        [cond("mfi", "gt", const(exit_min), spec)],
        f"MFI({length})<{entry_max:g}",
        f"MFI({length})>{exit_min:g}",
    )


def _obv(params: Params) -> Built:
    length = int(params["length"])
    return (
        [cond("obv", "gt", ref("obv_sma", {"length": length}))],
        [cond("obv", "lt", ref("obv_sma", {"length": length}))],
        f"OBV>SMA({length})",
        f"OBV<SMA({length})",
    )


def _bb_breakout(params: Params) -> Built:
    spec = {"length": int(params["length"]), "std": float(params["std"])}
    return (
        [cond("close", "gt", ref("bb_upper", spec))],
        [cond("close", "lt", ref("bb_mid", spec))],
        f"Close>BB({spec['length']},{spec['std']:.1f}) upper",
        f"Close<BB({spec['length']}) mid",
    )


def _bb(params: Params) -> Built:
    spec = {"length": int(params["length"]), "std": float(params["std"])}
    return (
        [cond("close", "lt", ref("bb_lower", spec))],
        [cond("close", "gt", ref("bb_mid", spec))],
        f"Close<BB({spec['length']},{spec['std']:.1f}) lower",
        f"Close>BB({spec['length']}) mid",
    )


def _kc_breakout(params: Params) -> Built:
    spec = {"length": int(params["length"]), "scalar": float(params["scalar"])}
    return (
        [cond("close", "gt", ref("kc_upper", spec))],
        [cond("close", "lt", ref("kc_mid", spec))],
        f"Close>KC({spec['length']},{spec['scalar']:.1f}) upper",
        f"Close<KC({spec['length']}) mid",
    )


def _kc(params: Params) -> Built:
    spec = {"length": int(params["length"]), "scalar": float(params["scalar"])}
    return (
        [cond("close", "lt", ref("kc_lower", spec))],
        [cond("close", "gt", ref("kc_mid", spec))],
        f"Close<KC({spec['length']},{spec['scalar']:.1f}) lower",
        f"Close>KC({spec['length']}) mid",
    )


def _donchian(params: Params) -> Built:
    length = int(params["length"])
    spec = {"length": length}
    return (
        [cond("close", "gt", ref("donchian_upper", spec))],
        [cond("close", "lt", ref("donchian_mid", spec))],
        f"Close>Donchian({length}) upper",
        f"Close<Donchian({length}) mid",
    )


def _atr(params: Params) -> Built:
    max_pct = float(params["max_pct"])
    return (
        [cond("atr_pct", "lt", const(max_pct), {"length": 14})],
        [],
        f"ATR%< {max_pct:.0%}",
        "",
    )


def _ema_filter(params: Params) -> Built:
    fast, slow = int(params["fast"]), int(params["slow"])
    return (
        [cond("ema", "gt", ref("ema", {"length": slow}), {"length": fast})],
        [],
        f"EMA({fast})>EMA({slow})",
        "",
    )


def _sma_filter(params: Params) -> Built:
    fast, slow = int(params["fast"]), int(params["slow"])
    return (
        [cond("sma", "gt", ref("sma", {"length": slow}), {"length": fast})],
        [],
        f"SMA({fast})>SMA({slow})",
        "",
    )


def _supertrend_filter(params: Params) -> Built:
    spec = {"length": int(params["length"]), "multiplier": float(params["multiplier"])}
    label = f"Supertrend({spec['length']},{spec['multiplier']:.1f})"
    return [cond("supertrend_dir", "gt", const(0), spec)], [], f"{label} up", ""


def _psar_filter(params: Params) -> Built:
    spec = {"af": float(params["af"]), "max_af": float(params["max_af"])}
    return [cond("psar_dir", "gt", const(0), spec)], [], "PSAR up", ""


def _aroon_filter(params: Params) -> Built:
    length = int(params["length"])
    spec = {"length": length}
    return (
        [cond("aroon_up", "gt", ref("aroon_down", spec), spec)],
        [],
        f"Aroon({length}) up",
        "",
    )


def _adx_filter(params: Params) -> Built:
    length = int(params["length"])
    minimum = float(params["minimum"])
    return (
        [cond("adx", "gt", const(minimum), {"length": length})],
        [],
        f"ADX({length})>{minimum:g}",
        "",
    )


def _rsi_filter(params: Params) -> Built:
    length = int(params["length"])
    minimum = float(params["minimum"])
    return (
        [cond("rsi", "gt", const(minimum), {"length": length})],
        [],
        f"RSI({length})>{minimum:g}",
        "",
    )


def _stoch_filter(params: Params) -> Built:
    spec = {"k": int(params["k"]), "d": int(params["d"]), "smooth": int(params["smooth"])}
    minimum = float(params["minimum"])
    return [cond("stoch_k", "gt", const(minimum), spec)], [], f"Stoch({spec['k']})>{minimum:g}", ""


def _macd_filter(params: Params) -> Built:
    spec = {
        "fast": int(params["fast"]),
        "slow": int(params["slow"]),
        "signal": int(params["signal"]),
    }
    text = f"MACD({spec['fast']},{spec['slow']},{spec['signal']})>0"
    return [cond("macd_hist", "gt", const(0), spec)], [], text, ""


def _roc_filter(params: Params) -> Built:
    length = int(params["length"])
    return [cond("roc", "gt", const(0), {"length": length})], [], f"ROC({length})>0", ""


def _cmf_filter(params: Params) -> Built:
    length = int(params["length"])
    return [cond("cmf", "gt", const(0), {"length": length})], [], f"CMF({length})>0", ""


def _obv_filter(params: Params) -> Built:
    length = int(params["length"])
    return (
        [cond("obv", "gt", ref("obv_sma", {"length": length}))],
        [],
        f"OBV>SMA({length})",
        "",
    )


def _mfi_filter(params: Params) -> Built:
    length = int(params["length"])
    minimum = float(params["minimum"])
    return (
        [cond("mfi", "gt", const(minimum), {"length": length})],
        [],
        f"MFI({length})>{minimum:g}",
        "",
    )


def _bb_filter(params: Params) -> Built:
    spec = {"length": int(params["length"]), "std": float(params["std"])}
    return (
        [cond("close", "gt", ref("bb_mid", spec))],
        [],
        f"Close>BB({spec['length']},{spec['std']:.1f}) mid",
        "",
    )


def _donchian_filter(params: Params) -> Built:
    length = int(params["length"])
    return (
        [cond("close", "gt", ref("donchian_mid", {"length": length}))],
        [],
        f"Close>Donchian({length}) mid",
        "",
    )


def _without(group: frozenset[str], ident: str) -> frozenset[str]:
    return group - {ident}


_MA = frozenset({"ema_stack", "sma_stack", "ema_filter", "sma_filter"})
_BANDS = frozenset({"bb_pullback", "bb_breakout", "bb_filter", "kc_pullback", "kc_breakout"})


@dataclass(frozen=True)
class Primitive:
    id: str
    family: str
    label: str
    kind: str
    oscillator_group: str | None
    correlated: frozenset[str]
    grid: tuple[Params, ...]
    build: Callable[[Params], Built]
    role: str
    claim: str | None
    series: str


PRIMITIVES: tuple[Primitive, ...] = (
    Primitive(
        "ema_stack",
        "trend",
        "EMA",
        "ma_pair",
        None,
        _without(_MA, "ema_stack"),
        _grid(
            {"fast": (10, 20), "slow": (50, 100)},
            lambda params: int(params["fast"]) < int(params["slow"]),
        ),
        _ema,
        "signal",
        "trend",
        "ema",
    ),
    Primitive(
        "sma_stack",
        "trend",
        "SMA",
        "ma_pair",
        None,
        _without(_MA, "sma_stack"),
        _grid(
            {"fast": (20, 50), "slow": (100, 200)},
            lambda params: int(params["fast"]) < int(params["slow"]),
        ),
        _sma,
        "signal",
        "trend",
        "sma",
    ),
    Primitive(
        "supertrend",
        "trend",
        "Supertrend",
        "other",
        None,
        frozenset(),
        _grid({"length": (10, 14), "multiplier": (2.0, 3.0)}),
        _supertrend,
        "signal",
        "trend",
        "supertrend",
    ),
    Primitive(
        "psar",
        "trend",
        "PSAR",
        "other",
        None,
        frozenset(),
        _grid({"af": (0.02,), "max_af": (0.2,)}),
        _psar,
        "signal",
        "trend",
        "psar",
    ),
    Primitive(
        "aroon",
        "trend",
        "Aroon",
        "other",
        None,
        frozenset(),
        _grid({"length": (14, 25)}),
        _aroon,
        "signal",
        "trend",
        "aroon",
    ),
    Primitive(
        "rsi_pullback",
        "momentum",
        "RSI",
        "oscillator",
        "bounded",
        frozenset(),
        _grid({"length": (14,), "entry_max": (30, 40), "exit_min": (60, 70)}),
        _rsi,
        "signal",
        "pullback",
        "rsi",
    ),
    Primitive(
        "rsi_continuation",
        "momentum",
        "RSI continuation",
        "oscillator",
        "bounded",
        frozenset(),
        _grid(
            {"length": (14,), "entry_min": (50, 55), "exit_max": (45, 50)},
            lambda params: float(params["exit_max"]) <= float(params["entry_min"]),
        ),
        _rsi_continuation,
        "signal",
        "continuation",
        "rsi",
    ),
    Primitive(
        "stoch_pullback",
        "momentum",
        "Stochastic",
        "oscillator",
        "bounded",
        frozenset(),
        _grid({"k": (14,), "d": (3,), "smooth": (3,), "entry_max": (20, 30), "exit_min": (70, 80)}),
        _stoch,
        "signal",
        "pullback",
        "stoch",
    ),
    Primitive(
        "macd_hist",
        "momentum",
        "MACD",
        "oscillator",
        None,
        frozenset(),
        _grid({"fast": (12,), "slow": (26,), "signal": (9,)}),
        _macd,
        "signal",
        "continuation",
        "macd",
    ),
    Primitive(
        "roc_sign",
        "momentum",
        "ROC",
        "oscillator",
        None,
        frozenset(),
        _grid({"length": (10, 20)}),
        _roc,
        "signal",
        "continuation",
        "roc",
    ),
    Primitive(
        "cmf_positive",
        "volume",
        "CMF",
        "other",
        None,
        frozenset(),
        _grid({"length": (20,)}),
        _cmf,
        "signal",
        "continuation",
        "cmf",
    ),
    Primitive(
        "mfi_pullback",
        "volume",
        "MFI",
        "oscillator",
        "bounded",
        frozenset(),
        _grid({"length": (14,), "entry_max": (20, 40), "exit_min": (70, 80)}),
        _mfi,
        "signal",
        "pullback",
        "mfi",
    ),
    Primitive(
        "obv_rising",
        "volume",
        "OBV",
        "other",
        None,
        frozenset(),
        _grid({"length": (20,)}),
        _obv,
        "signal",
        "continuation",
        "obv",
    ),
    Primitive(
        "bb_pullback",
        "volatility",
        "Bollinger",
        "channel",
        None,
        _without(_BANDS, "bb_pullback"),
        _grid({"length": (20,), "std": (2.0,)}),
        _bb,
        "signal",
        "fade",
        "bb",
    ),
    Primitive(
        "bb_breakout",
        "volatility",
        "Bollinger breakout",
        "channel",
        None,
        _without(_BANDS, "bb_breakout"),
        _grid({"length": (20,), "std": (2.0,)}),
        _bb_breakout,
        "signal",
        "breakout",
        "bb",
    ),
    Primitive(
        "kc_pullback",
        "volatility",
        "Keltner",
        "channel",
        None,
        _without(_BANDS, "kc_pullback"),
        _grid({"length": (20,), "scalar": (2.0,)}),
        _kc,
        "signal",
        "fade",
        "kc",
    ),
    Primitive(
        "kc_breakout",
        "volatility",
        "Keltner breakout",
        "channel",
        None,
        _without(_BANDS, "kc_breakout"),
        _grid({"length": (20,), "scalar": (2.0,)}),
        _kc_breakout,
        "signal",
        "breakout",
        "kc",
    ),
    Primitive(
        "donchian_breakout",
        "volatility",
        "Donchian",
        "channel",
        None,
        frozenset(),
        _grid({"length": (20, 55)}),
        _donchian,
        "signal",
        "breakout",
        "donchian",
    ),
    Primitive(
        "ema_filter",
        "trend",
        "EMA filter",
        "ma_pair",
        None,
        _without(_MA, "ema_filter"),
        _grid({"fast": (20,), "slow": (50,)}),
        _ema_filter,
        "filter",
        None,
        "ema",
    ),
    Primitive(
        "sma_filter",
        "trend",
        "SMA filter",
        "ma_pair",
        None,
        _without(_MA, "sma_filter"),
        _grid({"fast": (50,), "slow": (200,)}),
        _sma_filter,
        "filter",
        None,
        "sma",
    ),
    Primitive(
        "supertrend_filter",
        "trend",
        "Supertrend filter",
        "other",
        None,
        frozenset(),
        _grid({"length": (10,), "multiplier": (3.0,)}),
        _supertrend_filter,
        "filter",
        None,
        "supertrend",
    ),
    Primitive(
        "psar_filter",
        "trend",
        "PSAR filter",
        "other",
        None,
        frozenset(),
        _grid({"af": (0.02,), "max_af": (0.2,)}),
        _psar_filter,
        "filter",
        None,
        "psar",
    ),
    Primitive(
        "aroon_filter",
        "trend",
        "Aroon filter",
        "other",
        None,
        frozenset(),
        _grid({"length": (25,)}),
        _aroon_filter,
        "filter",
        None,
        "aroon",
    ),
    Primitive(
        "adx_filter",
        "trend",
        "ADX",
        "filter",
        None,
        frozenset(),
        _grid({"length": (14,), "minimum": (20,)}),
        _adx_filter,
        "filter",
        None,
        "adx",
    ),
    Primitive(
        "rsi_filter",
        "momentum",
        "RSI filter",
        "oscillator",
        "bounded",
        frozenset(),
        _grid({"length": (14,), "minimum": (50,)}),
        _rsi_filter,
        "filter",
        None,
        "rsi",
    ),
    Primitive(
        "stoch_filter",
        "momentum",
        "Stochastic filter",
        "oscillator",
        "bounded",
        frozenset(),
        _grid({"k": (14,), "d": (3,), "smooth": (3,), "minimum": (50,)}),
        _stoch_filter,
        "filter",
        None,
        "stoch",
    ),
    Primitive(
        "macd_filter",
        "momentum",
        "MACD filter",
        "oscillator",
        None,
        frozenset(),
        _grid({"fast": (12,), "slow": (26,), "signal": (9,)}),
        _macd_filter,
        "filter",
        None,
        "macd",
    ),
    Primitive(
        "roc_filter",
        "momentum",
        "ROC filter",
        "oscillator",
        None,
        frozenset(),
        _grid({"length": (20,)}),
        _roc_filter,
        "filter",
        None,
        "roc",
    ),
    Primitive(
        "cmf_filter",
        "volume",
        "CMF filter",
        "other",
        None,
        frozenset(),
        _grid({"length": (20,)}),
        _cmf_filter,
        "filter",
        None,
        "cmf",
    ),
    Primitive(
        "obv_filter",
        "volume",
        "OBV filter",
        "other",
        None,
        frozenset(),
        _grid({"length": (20,)}),
        _obv_filter,
        "filter",
        None,
        "obv",
    ),
    Primitive(
        "mfi_filter",
        "volume",
        "MFI filter",
        "oscillator",
        "bounded",
        frozenset(),
        _grid({"length": (14,), "minimum": (50,)}),
        _mfi_filter,
        "filter",
        None,
        "mfi",
    ),
    Primitive(
        "bb_filter",
        "volatility",
        "Bollinger filter",
        "channel",
        None,
        _without(_BANDS, "bb_filter"),
        _grid({"length": (20,), "std": (2.0,)}),
        _bb_filter,
        "filter",
        None,
        "bb",
    ),
    Primitive(
        "donchian_filter",
        "volatility",
        "Donchian filter",
        "channel",
        None,
        frozenset(),
        _grid({"length": (55,)}),
        _donchian_filter,
        "filter",
        None,
        "donchian",
    ),
    Primitive(
        "atr_regime",
        "volatility",
        "ATR",
        "filter",
        None,
        frozenset(),
        _grid({"max_pct": (0.08, 0.12)}),
        _atr,
        "filter",
        None,
        "atr",
    ),
)

BY_ID: dict[str, Primitive] = {primitive.id: primitive for primitive in PRIMITIVES}


def combination_allowed(chosen: tuple[Primitive, ...]) -> bool:
    if len({primitive.series for primitive in chosen}) != len(chosen):
        return False
    if sum(primitive.kind == "ma_pair" for primitive in chosen) > 1:
        return False
    bounded = [primitive for primitive in chosen if primitive.oscillator_group == "bounded"]
    if len(bounded) > 1:
        return False
    identifiers = {primitive.id for primitive in chosen}
    return all(not (identifiers & primitive.correlated) for primitive in chosen)


def parameter_axes() -> dict[str, list[int | float]]:
    from stocksweeper.strategy.exits import ATR_MULT, TIME_BARS

    axes: dict[str, list[int | float]] = {}
    for primitive in PRIMITIVES:
        for params in primitive.grid:
            for key, value in params.items():
                column = f"{primitive.id}_{key}"
                bucket = axes.setdefault(column, [])
                if value not in bucket:
                    bucket.append(value)
    axes["exit_time_bars"] = [TIME_BARS]
    axes["exit_atr_mult"] = [ATR_MULT]
    return axes
