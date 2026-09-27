"""Score recorded, as-issued intraday snapshots against exact matured closes.

Run from backend/: uv run --group research python scripts/evaluate_intraday_prospective.py
Only recorded watchlist snapshots form the denominator; this report never
promotes a model or calls retrospective current-vintage history as-issued.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from stocksweeper.config import load_settings
from stocksweeper.forecast.intraday_evidence import evaluate
from stocksweeper.forecast.ledger import ForecastLedger


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=load_settings().resolved_data_dir())
    parser.add_argument(
        "--since", type=date.fromisoformat, help="earliest completed-close input session"
    )
    args = parser.parse_args()
    rows = ForecastLedger(args.data_dir).evaluation_rows(provenance="as_issued", since=args.since)
    print(json.dumps(evaluate(rows), sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
