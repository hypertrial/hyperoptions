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


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())
