"""End-of-day report CLI.

Usage:
    uv run python -m app.scripts.eod_report             # today IST
    uv run python -m app.scripts.eod_report --date 2026-05-21
    uv run python -m app.scripts.eod_report --out path.md

Skips non-trading days unless --force is passed. Writes
reports/YYYY-MM-DD.md and prints the path.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

import structlog

from app.config import IST, get_settings
from app.journal.db import get_session_factory, init_db
from app.llm.client import GeminiClient
from app.llm.eod import run_eod_report
from app.util.calendar import is_trading_day, reason_for_closure

log = structlog.get_logger()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the EOD report.")
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="IST date to report on (YYYY-MM-DD). Defaults to today IST.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output file (default: reports/<date>.md).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Generate even on non-trading days (otherwise we skip with exit 0).",
    )
    return parser.parse_args()


async def main_async() -> int:
    args = _parse_args()
    target = args.date or datetime.now(IST).date()

    if not is_trading_day(target) and not args.force:
        print(
            f"skipping: {target} is a non-trading day ({reason_for_closure(target)}). "
            "use --force to override.",
            file=sys.stderr,
        )
        return 0

    settings = get_settings()
    if not settings.gemini_api_key:
        print(
            "GEMINI_API_KEY not set in .env; cannot generate report.",
            file=sys.stderr,
        )
        return 2

    await init_db()
    sf = get_session_factory()
    client = GeminiClient(sf)
    try:
        _, path = await run_eod_report(
            client=client,
            session_factory=sf,
            ist_date=target,
            out_path=args.out,
        )
    except Exception as e:
        print(f"EOD report failed: {e}", file=sys.stderr)
        return 3
    print(str(path))
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
