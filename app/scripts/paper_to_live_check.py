"""Phase 7 go/no-go analyzer.

Usage:
    uv run python -m app.scripts.paper_to_live_check
    uv run python -m app.scripts.paper_to_live_check --start 2026-05-21 --end 2026-06-03
    uv run python -m app.scripts.paper_to_live_check --format json
    uv run python -m app.scripts.paper_to_live_check --out reports/paper_check.md

Exit codes:
    0  all three gates passed (win rate, expectancy, max drawdown)
    1  at least one gate failed
    2  no trades in the window (cannot evaluate)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from app.analytics.paper_run import compute_paper_run_stats, format_text
from app.config import IST, get_settings
from app.journal.db import get_session_factory, init_db


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper-run statistics + Phase 7 go/no-go gates.")
    parser.add_argument(
        "--start",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Start IST date (inclusive). Defaults to 14 days before today IST.",
    )
    parser.add_argument(
        "--end",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="End IST date (inclusive). Defaults to today IST.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write to this path instead of stdout.",
    )
    return parser.parse_args()


async def main_async() -> int:
    args = _parse_args()
    today_ist = datetime.now(IST).date()
    end_ist: date = args.end or today_ist
    start_ist: date = args.start or (end_ist - timedelta(days=14))

    settings = get_settings()
    await init_db()
    sf = get_session_factory()

    stats = await compute_paper_run_stats(
        sf,
        start_ist=start_ist,
        end_ist=end_ist,
        capital_inr=settings.capital_inr,
    )

    if args.format == "json":
        rendered = json.dumps(stats.to_dict(), indent=2, default=str)
    else:
        rendered = format_text(stats)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n")
        print(str(args.out))
    else:
        print(rendered)

    if stats.total_trades == 0:
        return 2
    return 0 if stats.overall_pass else 1


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
