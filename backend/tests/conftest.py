from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_research_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCKSWEEPER_DATA_DIR", str(tmp_path / "research"))


@pytest.fixture(autouse=True)
def reset_treasury_acquisition_state() -> None:
    import options_api.market_sources as sources

    sources._treasury_cache = None
    sources._treasury_failed_at = None
    yield
    sources._treasury_cache = None
    sources._treasury_failed_at = None


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())
