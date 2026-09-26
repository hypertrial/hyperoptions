"""Local technical strategy backtester."""

from __future__ import annotations

import os

__version__ = "0.1.0"


def _configure_numba_cache() -> None:
    if os.environ.get("NUMBA_CACHE_DIR"):
        return
    from stocksweeper.config import load_settings

    cache = load_settings().resolved_data_dir() / "numba"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["NUMBA_CACHE_DIR"] = str(cache)


_configure_numba_cache()
