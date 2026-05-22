"""Backtest the ORB strategy over a window of trading days.

Usage:
    uv run python -m app.scripts.backtest_orb              # last 20 trading days, excluding today
    uv run python -m app.scripts.backtest_orb --end 2026-05-20 --sessions 20
    uv run python -m app.scripts.backtest_orb --out reports/backtest-orb-custom.md

Fetches historical 1-min bars via Angel One SmartAPI (with disk cache),
replays them through ORBStrategy + simulated execution, and writes a
detailed markdown report.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import structlog

from app.backtest.historical import (
    authed_smart_connect,
    fetch_universe_for_dates,
    list_trading_dates_back,
)
from app.backtest.replay import run_backtest
from app.backtest.report import format_backtest_report
from app.config import IST, PROJECT_ROOT
from app.util.calendar import is_trading_day

log = structlog.get_logger()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest the ORB strategy.")
    parser.add_argument(
        "--end",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Last IST date (inclusive) for the backtest. Defaults to yesterday IST.",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=20,
        help="Number of trading sessions to backtest (default 20).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output markdown path (default: reports/backtest-orb-<start>-to-<end>.md).",
    )
    return parser.parse_args()


def _default_end(today_ist: date) -> date:
    # "Yesterday IST" — walk backwards over weekends/holidays.
    d = today_ist - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


async def main_async() -> int:
    args = _parse_args()
    today_ist = datetime.now(IST).date()
    end_ist: date = args.end or _default_end(today_ist)

    if not is_trading_day(end_ist):
        print(f"--end {end_ist} is not a trading day", file=sys.stderr)
        return 2

    dates = list_trading_dates_back(end_ist, args.sessions)
    if len(dates) < args.sessions:
        print(
            f"warning: requested {args.sessions} sessions, only found {len(dates)} "
            f"({dates[0]} → {dates[-1]})",
            file=sys.stderr,
        )

    print(f"backtest window: {dates[0]} → {dates[-1]} ({len(dates)} trading days)")

    session = authed_smart_connect()
    print("fetching historical bars (cached per (symbol, date)):")
    bars_by_date = fetch_universe_for_dates(session, dates)
    total_bars = sum(len(b) for day in bars_by_date.values() for b in day.values())
    print(f"  → {total_bars} bars across {len(dates)} days × 5 symbols")

    print("replaying...")
    result = run_backtest(bars_by_date)
    print(f"  → {len(result.trades)} trades across {len(result.sessions)} sessions")

    out_path = args.out or (PROJECT_ROOT / "reports" / f"backtest-orb-{dates[0]}-to-{dates[-1]}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(format_backtest_report(result))
    print(str(out_path))
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
