"""ORB parameter sweep — runs the backtest engine over a grid of strategy
configurations against the same cached historical bars.

Usage:
    uv run python -m app.scripts.backtest_sweep
    uv run python -m app.scripts.backtest_sweep --end 2026-05-20 --sessions 20
    uv run python -m app.scripts.backtest_sweep --out reports/sweep.md

Grid (3 × 3 × 3 = 27 configs):
    or_window_minutes  : 15, 30, 45
    volume_multiplier  : 1.5, 2.0, 2.5
    target_r_multiple  : 1.0, 1.5, 2.0

Ranks by (passes all 3 Phase 7 gates, then expectancy descending).
No API calls — relies on the cache populated by `backtest_orb`.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from app.analytics.paper_run import (
    EXPECTANCY_THRESHOLD_INR,
    MAX_DRAWDOWN_THRESHOLD_PCT,
    WIN_RATE_THRESHOLD_PCT,
)
from app.backtest.historical import list_trading_dates_back, load_universe_from_cache
from app.backtest.replay import BacktestResult, run_backtest
from app.config import IST, PROJECT_ROOT
from app.util.calendar import is_trading_day

OR_WINDOWS = (15, 30, 45)
VOL_MULTIPLIERS = (1.5, 2.0, 2.5)
TARGET_RS = (1.0, 1.5, 2.0)


@dataclass(frozen=True, slots=True)
class SweepRow:
    or_window: int
    vol_mult: float
    target_r: float
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    total_pnl: float
    expectancy: float
    avg_r: float
    max_dd_pct: float
    pass_win_rate: bool
    pass_expectancy: bool
    pass_drawdown: bool

    @property
    def overall_pass(self) -> bool:
        return self.pass_win_rate and self.pass_expectancy and self.pass_drawdown

    @property
    def gates_passed_count(self) -> int:
        return sum([self.pass_win_rate, self.pass_expectancy, self.pass_drawdown])


def _summarize(r: BacktestResult, *, or_window: int, vol_mult: float, target_r: float) -> SweepRow:
    trades = r.trades
    n = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    total = sum(t.net_pnl for t in trades)
    win_rate = (100.0 * len(wins) / n) if n else 0.0
    expectancy = (total / n) if n else 0.0
    avg_r = (sum(t.r_multiple for t in trades) / n) if n else 0.0

    # Max drawdown
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for t in trades:
        equity += t.net_pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    max_dd_pct = 100.0 * worst / r.capital_inr if r.capital_inr > 0 else 0.0

    return SweepRow(
        or_window=or_window,
        vol_mult=vol_mult,
        target_r=target_r,
        trades=n,
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=round(win_rate, 2),
        total_pnl=round(total, 2),
        expectancy=round(expectancy, 2),
        avg_r=round(avg_r, 3),
        max_dd_pct=round(max_dd_pct, 2),
        pass_win_rate=n > 0 and win_rate >= WIN_RATE_THRESHOLD_PCT,
        pass_expectancy=n > 0 and expectancy > EXPECTANCY_THRESHOLD_INR,
        pass_drawdown=max_dd_pct <= MAX_DRAWDOWN_THRESHOLD_PCT,
    )


def format_sweep_report(rows: list[SweepRow], start_ist: str, end_ist: str) -> str:
    sorted_rows = sorted(
        rows,
        key=lambda r: (
            -r.gates_passed_count,
            -r.expectancy,
            -r.win_rate_pct,
        ),
    )
    L: list[str] = []
    L.append(f"# ORB Parameter Sweep — {start_ist} → {end_ist}")
    L.append("")
    L.append(
        f"_{len(rows)} configs × {len(sorted_rows[0].__dataclass_fields__) if rows else 0} "
        "fields. Same cached bars, same fill model, varying only strategy parameters._"
    )
    L.append("")
    passing = [r for r in sorted_rows if r.overall_pass]
    L.append(f"**{len(passing)} of {len(sorted_rows)} configs pass all three gates.**")
    L.append("")
    L.append("## Ranked results (best first)")
    L.append("")
    L.append(
        "| OR | Vol× | TgtR | Trades | W/L | Win% | Net P&L | Expectancy "
        "| Avg R | Max DD% | Gates | Verdict |"
    )
    L.append(
        "| ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: | ---: | :---: | :---: |"
    )
    for r in sorted_rows:
        gates = (
            ("✓" if r.pass_win_rate else "✗")
            + ("✓" if r.pass_expectancy else "✗")
            + ("✓" if r.pass_drawdown else "✗")
        )
        verdict = "**GO**" if r.overall_pass else "no-go"
        L.append(
            f"| {r.or_window}m | {r.vol_mult:.1f} | {r.target_r:.1f} | {r.trades} "
            f"| {r.wins}/{r.losses} | {r.win_rate_pct:.1f}% "
            f"| ₹{r.total_pnl:+,.2f} | ₹{r.expectancy:+,.2f} "
            f"| {r.avg_r:+.2f} | {r.max_dd_pct:.2f}% | {gates} | {verdict} |"
        )
    L.append("")
    L.append("_Gates column = win-rate ✓✗ / expectancy ✓✗ / drawdown ✓✗_")
    L.append(
        f"_Thresholds: win-rate ≥ {WIN_RATE_THRESHOLD_PCT}%, "
        f"expectancy > ₹{EXPECTANCY_THRESHOLD_INR}, max DD ≤ {MAX_DRAWDOWN_THRESHOLD_PCT}%_"
    )
    return "\n".join(L)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parameter sweep for ORBStrategy.")
    parser.add_argument(
        "--end",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="End IST date (inclusive). Defaults to yesterday IST.",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=20,
        help="Trading sessions to evaluate (default 20).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output markdown path (default: reports/sweep-orb-<start>-to-<end>.md).",
    )
    return parser.parse_args()


def _default_end(today_ist: date) -> date:
    d = today_ist - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


async def main_async() -> int:
    args = _parse_args()
    today_ist = datetime.now(IST).date()
    end_ist: date = args.end or _default_end(today_ist)
    dates = list_trading_dates_back(end_ist, args.sessions)
    if not dates:
        print("no trading dates found", file=sys.stderr)
        return 2

    print(f"sweep window: {dates[0]} → {dates[-1]} ({len(dates)} sessions)")
    print("loading cached bars (no API)…")
    bars_by_date = load_universe_from_cache(dates)
    print("  ok.")

    rows: list[SweepRow] = []
    combos = list(itertools.product(OR_WINDOWS, VOL_MULTIPLIERS, TARGET_RS))
    print(f"running {len(combos)} configs…")
    for i, (orw, vm, tr) in enumerate(combos, start=1):
        kwargs: dict[str, object] = {
            "or_window_minutes": orw,
            "volume_multiplier": vm,
            "target_r_multiple": tr,
        }
        result = run_backtest(bars_by_date, orb_kwargs=kwargs)
        row = _summarize(result, or_window=orw, vol_mult=vm, target_r=tr)
        rows.append(row)
        marker = "GO " if row.overall_pass else "   "
        print(
            f"  [{i:2d}/{len(combos)}] {marker}  OR={orw}m vol×={vm:.1f} tgtR={tr:.1f}  "
            f"trades={row.trades:2d}  win={row.win_rate_pct:5.1f}%  "
            f"exp=₹{row.expectancy:+7.2f}  DD={row.max_dd_pct:5.2f}%"
        )

    out_path = args.out or (PROJECT_ROOT / "reports" / f"sweep-orb-{dates[0]}-to-{dates[-1]}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(format_sweep_report(rows, dates[0].isoformat(), dates[-1].isoformat()))
    print()
    print(str(out_path))
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
