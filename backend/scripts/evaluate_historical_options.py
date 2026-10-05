"""Freeze collected local sources, then replay all historical option studies."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# Fix aggregation/BLAS parallelism before importing numerical libraries. This
# governs reproducible replay, never model fit acceptance or iteration limits.
os.environ["POLARS_MAX_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"

from stocksweeper.research.historical_options.runner import run_snapshot
from stocksweeper.research.historical_options.snapshot import freeze_snapshot


def main() -> None:
    repo = Path(__file__).resolve().parents[2]
    root = repo / ".local/research/historical-options"
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="Validate and freeze existing local sources")
    freeze.add_argument("--source", type=Path, default=repo / "research/massive_options.duckdb")
    freeze.add_argument("--prices", type=Path, default=repo / ".local/research/forecast/prices")
    freeze.add_argument("--flat-cache", type=Path, default=repo / "research/.dlt/flatfiles")
    freeze.add_argument("--output", type=Path, default=root / "snapshot-v1")
    run = commands.add_parser("run", help="Verify a snapshot and execute all three studies")
    run.add_argument("--snapshot", type=Path, default=root / "snapshot-v1")
    run.add_argument("--output", type=Path, default=root / "run-v1")
    wheel = commands.add_parser("wheel", help="Research characteristics for the next wheel leg")
    wheel.add_argument("--snapshot", type=Path, default=root / "snapshot-v2")
    wheel.add_argument("--output", type=Path, default=root / "wheel-v1")
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            result = freeze_snapshot(args.source, args.prices, args.flat_cache, args.output)
        elif args.command == "run":
            result = run_snapshot(args.snapshot, args.output)
        else:
            from stocksweeper.research.historical_options.wheel import run_wheel

            result = run_wheel(args.snapshot, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Research command failed: {exc}\n")
    print(json.dumps({
        "kind": result.get("kind", result.get("artifact_type")),
        "canonical_hash": result["canonical_hash"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
