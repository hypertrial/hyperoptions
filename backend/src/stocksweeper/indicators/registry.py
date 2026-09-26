"""Curated pandas-ta-classic indicators plus a rolling VWAP.

Every output is a function of bars at or before the current close. Donchian
channels are shifted by one bar so a breakout can actually cross a prior
extreme; the unshifted channel is always at least as high as the current high.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from stocksweeper.indicators.names import IndicatorRequest, column_name, declared_columns


def _starts(frame: pd.DataFrame, prefix: str) -> pd.Series:
    columns = [column for column in frame.columns if column.startswith(prefix)]
    if len(columns) != 1:
        raise RuntimeError(f"expected one {prefix} column, found {columns}")
    series = frame[columns[0]]
    if not isinstance(series, pd.Series):
        raise RuntimeError(f"expected a series for {columns[0]}")
    return series


def _prior(series: pd.Series) -> pd.Series:
    shifted = series.shift(1)
    if not isinstance(shifted, pd.Series):
        raise RuntimeError("expected a shifted series")
    return shifted


def _ema(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    length = int(params["length"])
    return {column_name("ema", params): ta.ema(df["close"], length=length)}


def _sma(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    length = int(params["length"])
    return {column_name("sma", params): ta.sma(df["close"], length=length)}


def _rsi(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    length = int(params["length"])
    return {column_name("rsi", params): ta.rsi(df["close"], length=length)}


def _macd(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    raw = ta.macd(
        df["close"],
        fast=int(params["fast"]),
        slow=int(params["slow"]),
        signal=int(params["signal"]),
    )
    return {column_name("macd", params, "hist"): _starts(raw, "MACDh_")}


def _adx(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    raw = ta.adx(df["high"], df["low"], df["close"], length=int(params["length"]))
    return {column_name("adx", params): _starts(raw, "ADX_")}


def _supertrend(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    raw = ta.supertrend(
        df["high"],
        df["low"],
        df["close"],
        length=int(params["length"]),
        multiplier=float(params["multiplier"]),
    )
    direction = _starts(raw, "SUPERTd_")
    return {column_name("supertrend", params, "dir"): direction}


def _roc(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    length = int(params["length"])
    return {column_name("roc", params): ta.roc(df["close"], length=length)}


def _stoch(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    raw = ta.stoch(
        df["high"],
        df["low"],
        df["close"],
        k=int(params["k"]),
        d=int(params["d"]),
        smooth_k=int(params["smooth"]),
    )
    return {column_name("stoch", params, "k"): _starts(raw, "STOCHk_")}


def _bb(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    raw = ta.bbands(df["close"], length=int(params["length"]), std=float(params["std"]))
    return {
        column_name("bb", params, "lower"): _starts(raw, "BBL_"),
        column_name("bb", params, "mid"): _starts(raw, "BBM_"),
        column_name("bb", params, "upper"): _starts(raw, "BBU_"),
    }


def _atr(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    atr = ta.atr(df["high"], df["low"], df["close"], length=int(params["length"]))
    return {
        column_name("atr", params): atr,
        column_name("atr", params, "pct"): atr / df["close"],
    }


def _kc(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    raw = ta.kc(
        df["high"],
        df["low"],
        df["close"],
        length=int(params["length"]),
        scalar=float(params["scalar"]),
    )
    return {
        column_name("kc", params, "lower"): _starts(raw, "KCLe_"),
        column_name("kc", params, "mid"): _starts(raw, "KCBe_"),
        column_name("kc", params, "upper"): _starts(raw, "KCUe_"),
    }


def _donchian(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    length = int(params["length"])
    raw = ta.donchian(df["high"], df["low"], lower_length=length, upper_length=length)
    return {
        column_name("donchian", params, "lower"): _prior(_starts(raw, "DCL_")),
        column_name("donchian", params, "mid"): _prior(_starts(raw, "DCM_")),
        column_name("donchian", params, "upper"): _prior(_starts(raw, "DCU_")),
    }


def _aroon(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    raw = ta.aroon(df["high"], df["low"], length=int(params["length"]))
    return {
        column_name("aroon", params, "up"): _starts(raw, "AROONU_"),
        column_name("aroon", params, "down"): _starts(raw, "AROOND_"),
    }


def _psar(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    raw = ta.psar(
        df["high"],
        df["low"],
        df["close"],
        af0=float(params["af"]),
        af=float(params["af"]),
        max_af=float(params["max_af"]),
    )
    long_stop = _starts(raw, "PSARl_")
    short_stop = _starts(raw, "PSARs_")
    direction = pd.Series(
        np.where(long_stop.notna(), 1.0, np.where(short_stop.notna(), -1.0, np.nan)),
        index=df.index,
    )
    return {column_name("psar", params, "dir"): direction}


def _obv(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    del params
    return {"obv": ta.obv(df["close"], df["volume"])}


def _obv_sma(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    obv = ta.obv(df["close"], df["volume"])
    return {
        "obv": obv,
        column_name("obv_sma", params): obv.rolling(int(params["length"])).mean(),
    }


def _cmf(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    length = int(params["length"])
    series = ta.cmf(df["high"], df["low"], df["close"], df["volume"], length=length)
    return {column_name("cmf", params): series}


def _mfi(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    length = int(params["length"])
    series = ta.mfi(df["high"], df["low"], df["close"], df["volume"], length=length)
    return {column_name("mfi", params): series}


def _vwap(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    length = int(params["length"])
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].astype(float)
    rolled = (typical * volume).rolling(length).sum() / volume.rolling(length).sum()
    return {column_name("vwap", params): rolled}


def _zscore(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    length = int(params["length"])
    return {column_name("zscore", params): ta.zscore(df["close"], length=length)}


def _cdl_doji(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    series = ta.cdl_doji(
        df["open"],
        df["high"],
        df["low"],
        df["close"],
        length=int(params["length"]),
        factor=float(params["factor"]),
    )
    return {column_name("cdl_doji", params): series}


def _cdl_inside(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    del params
    return {"cdl_inside": ta.cdl_inside(df["open"], df["high"], df["low"], df["close"])}


def _cdl_z(df: pd.DataFrame, params: dict[str, int | float]) -> dict[str, pd.Series]:
    import pandas_ta_classic as ta

    raw = ta.cdl_z(df["open"], df["high"], df["low"], df["close"], length=int(params["length"]))
    close_column = [column for column in raw.columns if column.startswith("close_Z_")]
    if len(close_column) != 1:
        raise RuntimeError(f"expected one close z-score column, found {list(raw.columns)}")
    return {column_name("cdl_z", params): raw[close_column[0]]}


Compute = Callable[[pd.DataFrame, dict[str, int | float]], dict[str, pd.Series]]

SPECS: dict[str, Compute] = {
    "ema": _ema,
    "sma": _sma,
    "rsi": _rsi,
    "macd": _macd,
    "adx": _adx,
    "supertrend": _supertrend,
    "roc": _roc,
    "stoch": _stoch,
    "bb": _bb,
    "atr": _atr,
    "kc": _kc,
    "donchian": _donchian,
    "aroon": _aroon,
    "psar": _psar,
    "obv": _obv,
    "obv_sma": _obv_sma,
    "cmf": _cmf,
    "mfi": _mfi,
    "vwap": _vwap,
    "zscore": _zscore,
    "cdl_doji": _cdl_doji,
    "cdl_inside": _cdl_inside,
    "cdl_z": _cdl_z,
}


def curated_requests() -> list[IndicatorRequest]:
    """One representative parameterization of every registered indicator."""
    return [
        IndicatorRequest.make("ema", {"length": 20}),
        IndicatorRequest.make("sma", {"length": 50}),
        IndicatorRequest.make("rsi", {"length": 14}),
        IndicatorRequest.make("macd", {"fast": 12, "slow": 26, "signal": 9}),
        IndicatorRequest.make("adx", {"length": 14}),
        IndicatorRequest.make("supertrend", {"length": 10, "multiplier": 3.0}),
        IndicatorRequest.make("roc", {"length": 10}),
        IndicatorRequest.make("stoch", {"k": 14, "d": 3, "smooth": 3}),
        IndicatorRequest.make("bb", {"length": 20, "std": 2.0}),
        IndicatorRequest.make("atr", {"length": 14}),
        IndicatorRequest.make("kc", {"length": 20, "scalar": 2.0}),
        IndicatorRequest.make("donchian", {"length": 20}),
        IndicatorRequest.make("aroon", {"length": 14}),
        IndicatorRequest.make("psar", {"af": 0.02, "max_af": 0.2}),
        IndicatorRequest.make("obv", {}),
        IndicatorRequest.make("obv_sma", {"length": 20}),
        IndicatorRequest.make("cmf", {"length": 20}),
        IndicatorRequest.make("mfi", {"length": 14}),
        IndicatorRequest.make("vwap", {"length": 20}),
        IndicatorRequest.make("zscore", {"length": 20}),
        IndicatorRequest.make("cdl_doji", {"length": 10, "factor": 0.1}),
        IndicatorRequest.make("cdl_inside", {}),
        IndicatorRequest.make("cdl_z", {"length": 30}),
    ]


def output_columns(request: IndicatorRequest) -> list[str]:
    return declared_columns(request.name, request.as_dict())
