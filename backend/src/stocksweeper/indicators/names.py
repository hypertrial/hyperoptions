"""Deterministic indicator column names shared by the engine and the compiler."""

from __future__ import annotations

from dataclasses import dataclass

PRICE_COLUMNS = frozenset({"open", "high", "low", "close", "volume"})


@dataclass(frozen=True)
class IndicatorDecl:
    """One indicator. Column suffixes use ``""`` for the unsuffixed column.

    ``hidden`` suffixes are still written to the cache but are not strategy
    signals. ``extra`` columns are copied from another indicator's output.
    """

    params: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ("",)
    extra: tuple[str, ...] = ()
    hidden: frozenset[str] = frozenset()


# macd parameter order is alphabetical. That is the order already stored in caches.
INDICATORS: dict[str, IndicatorDecl] = {
    "ema": IndicatorDecl(("length",)),
    "sma": IndicatorDecl(("length",)),
    "rsi": IndicatorDecl(("length",)),
    "macd": IndicatorDecl(("fast", "signal", "slow"), suffixes=("hist",)),
    "adx": IndicatorDecl(("length",)),
    "supertrend": IndicatorDecl(("length", "multiplier"), suffixes=("dir",)),
    "roc": IndicatorDecl(("length",)),
    "stoch": IndicatorDecl(("k", "d", "smooth"), suffixes=("k",)),
    "bb": IndicatorDecl(("length", "std"), suffixes=("lower", "mid", "upper")),
    "atr": IndicatorDecl(("length",), suffixes=("", "pct"), hidden=frozenset({""})),
    "kc": IndicatorDecl(("length", "scalar"), suffixes=("lower", "mid", "upper")),
    "donchian": IndicatorDecl(("length",), suffixes=("lower", "mid", "upper")),
    "aroon": IndicatorDecl(("length",), suffixes=("up", "down")),
    "psar": IndicatorDecl(("af", "max_af"), suffixes=("dir",)),
    "obv": IndicatorDecl(),
    "obv_sma": IndicatorDecl(("length",), extra=("obv",)),
    "cmf": IndicatorDecl(("length",)),
    "mfi": IndicatorDecl(("length",)),
    "vwap": IndicatorDecl(("length",)),
    "zscore": IndicatorDecl(("length",)),
    "cdl_doji": IndicatorDecl(("length", "factor")),
    "cdl_inside": IndicatorDecl(),
    "cdl_z": IndicatorDecl(("length",)),
}

PARAM_ORDER: dict[str, tuple[str, ...]] = {name: decl.params for name, decl in INDICATORS.items()}


def _signal(name: str, suffix: str) -> str:
    return name if suffix == "" else f"{name}_{suffix}"


SUFFIXES: dict[str, tuple[str, str]] = {
    _signal(name, suffix): (name, suffix)
    for name, decl in INDICATORS.items()
    for suffix in decl.suffixes
    if suffix and suffix not in decl.hidden
}

REQUEST_OF: dict[str, str] = {
    _signal(name, suffix): name
    for name, decl in INDICATORS.items()
    for suffix in decl.suffixes
    if suffix not in decl.hidden
}


def format_param(value: int | float) -> str:
    number = float(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if number.is_integer():
        return f"{number:.1f}"
    return f"{number:.4f}".rstrip("0").rstrip(".")


def column_name(name: str, params: dict[str, int | float], suffix: str = "") -> str:
    order = PARAM_ORDER.get(name, tuple(sorted(params)))
    parts = [name]
    seen: set[str] = set()
    for key in order:
        if key in params:
            parts.append(format_param(params[key]))
            seen.add(key)
    for key in sorted(params):
        if key not in seen:
            parts.append(format_param(params[key]))
    if suffix:
        parts.append(suffix)
    return "_".join(parts)


def declared_columns(name: str, params: dict[str, int | float]) -> list[str]:
    decl = INDICATORS[name]
    columns = [column_name(name, params, suffix) for suffix in decl.suffixes]
    columns.extend(decl.extra)
    return columns


def column_for(indicator: str, params: dict[str, int | float]) -> str:
    if indicator in PRICE_COLUMNS:
        return indicator
    if indicator in SUFFIXES:
        base, suffix = SUFFIXES[indicator]
        return column_name(base, params, suffix)
    return column_name(indicator, params)


@dataclass(frozen=True)
class IndicatorRequest:
    name: str
    params: tuple[tuple[str, int | float], ...]

    @classmethod
    def make(cls, name: str, params: dict[str, int | float] | None = None) -> IndicatorRequest:
        items = tuple(sorted((key, _number(value)) for key, value in (params or {}).items()))
        return cls(name, items)

    def as_dict(self) -> dict[str, int | float]:
        return dict(self.params)


def _number(value: int | float) -> int | float:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    number = float(value)
    if number.is_integer() and abs(number) >= 1:
        return int(number) if not isinstance(value, float) else value
    return number


def request_for(indicator: str, params: dict[str, int | float]) -> IndicatorRequest | None:
    if indicator in PRICE_COLUMNS:
        return None
    name = REQUEST_OF.get(indicator)
    if name is None:
        raise KeyError(f"unknown indicator {indicator}")
    return IndicatorRequest.make(name, params)
