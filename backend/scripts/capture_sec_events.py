"""Capture explicit earnings dates from a bounded set of one issuer's recent 8-Ks."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from stocksweeper.config import load_settings
from stocksweeper.forecast.sec_events import capture_recent_sec_schedules


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="Exact SEC-listed ticker, e.g. AAPL")
    parser.add_argument("cik", type=int, help="SEC CIK for that ticker")
    parser.add_argument("--user-agent", help="Declared SEC contact (or use environment variable)")
    parser.add_argument("--data-dir", type=Path, help="override STOCKSWEEPER_DATA_DIR")
    args = parser.parse_args()
    user_agent = args.user_agent or os.environ.get("HYPEROPTIONS_SEC_USER_AGENT")
    if not user_agent:
        parser.error("set HYPEROPTIONS_SEC_USER_AGENT to a declared name and contact email")
    added = capture_recent_sec_schedules(
        args.data_dir or load_settings().resolved_data_dir(),
        args.ticker.upper(),
        args.cik,
        user_agent,
    )
    print(f"forward schedules added: {added}; actual releases are recorded separately")


if __name__ == "__main__":
    main()
