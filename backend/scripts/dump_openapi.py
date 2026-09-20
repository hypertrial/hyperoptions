from __future__ import annotations

import json
from pathlib import Path

from options_api.main import app

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "frontend" / "openapi.json"


def dump_openapi() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(app.openapi(), indent=2) + "\n")


if __name__ == "__main__":
    dump_openapi()
    print(OUTPUT)
