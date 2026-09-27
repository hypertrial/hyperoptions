"""Freeze 50 verified cached Nasdaq stocks for current-vintage replay screening.

Usage: uv run python scripts/freeze_audit_cohort.py [--as-of YYYY-MM-DD]
This never reconstructs historical forecasts as if they had been issued then.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path

from options_api.nasdaq import create_http_client
from options_api.universe import TickerUniverse
from stocksweeper.config import load_settings
from stocksweeper.forecast.audit import freeze_audit_cohort, read_audit_cohort
from stocksweeper.forecast.calendar import SessionCalendar


async def _eligible_nasdaq_stocks() -> list[str]:
    async with create_http_client() as client:
        universe = TickerUniverse(client)
        if not await universe.ensure():
            raise ValueError("Nasdaq screener is unavailable; audit cohort was not created")
        return [listing.symbol for listing in universe.forecast_peer_listings()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="override STOCKSWEEPER_DATA_DIR")
    parser.add_argument("--as-of", type=date.fromisoformat, help="completed cache session")
    args = parser.parse_args()
    data_dir = args.data_dir or load_settings().resolved_data_dir()
    calendar = SessionCalendar()
    completed = args.as_of or calendar.last_completed(datetime.now(UTC))
    if completed > calendar.last_completed(datetime.now(UTC)):
        parser.error("--as-of must be a completed trading session")
    cohort = read_audit_cohort(data_dir)
    if cohort is None:
        try:
            eligible = asyncio.run(_eligible_nasdaq_stocks())
            cohort = freeze_audit_cohort(data_dir, eligible, completed, calendar=calendar)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.as_of is not None and cohort.completed_session != completed:
        parser.error("fixed audit cohort already exists for a different completed session")
    print(
        json.dumps(
            {
                "provenance": cohort.provenance,
                "completed_session": cohort.completed_session.isoformat(),
                "frozen_at": cohort.frozen_at.isoformat(),
                "size": len(cohort.members),
                "tickers": [member.ticker for member in cohort.members],
            },
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
