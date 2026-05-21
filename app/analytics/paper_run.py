"""Paper-run statistics for the Phase 7 go/no-go check.

Pulls closed positions, fills, slippage logs, regime verdicts, and risk blocks
from the journal for an IST date window and shapes them into a `PaperRunStats`
dataclass that the CLI renders as text or JSON.

Three gates from Section 9 of the brief decide whether to graduate to live:
    * Win rate >= 40%
    * Expectancy per trade > 0 (net of charges — `realised_pnl` already nets them)
    * Max drawdown <= 8% of capital
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import IST
from app.journal import models as m

WIN_RATE_THRESHOLD_PCT = 40.0
EXPECTANCY_THRESHOLD_INR = 0.0
MAX_DRAWDOWN_THRESHOLD_PCT = 8.0


@dataclass(frozen=True, slots=True)
class TradeRow:
    """One closed position with its derived risk + R-multiple."""

    position_id: int
    symbol: str
    direction: str
    qty: int
    entry_price: float
    closed_at_ist: str
    realised_pnl: float
    risk_amount_inr: float | None  # qty * stop_distance; None if signal not found
    r_multiple: float | None


@dataclass(frozen=True, slots=True)
class PaperRunStats:
    period_start_ist: str
    period_end_ist: str
    trading_days: int

    # Trade counts
    total_trades: int
    wins: int
    losses: int
    breakevens: int

    # P&L
    total_pnl_inr: float
    avg_winner_inr: float | None
    avg_loser_inr: float | None
    largest_winner_inr: float | None
    largest_loser_inr: float | None
    expectancy_per_trade_inr: float | None
    win_rate_pct: float | None
    avg_r_multiple: float | None

    # Drawdown
    max_drawdown_inr: float
    max_drawdown_pct: float  # of capital

    # Slippage
    fills_count: int
    avg_slippage_bps: float | None
    max_abs_slippage_bps: float | None

    # Regime gating
    regime_calls: int
    risk_off_high_confidence_calls: int
    signals_blocked_by_regime: int

    # Detail tables
    trades: list[TradeRow] = field(default_factory=list)

    # Gate results
    pass_win_rate: bool = False
    pass_expectancy: bool = False
    pass_drawdown: bool = False

    @property
    def overall_pass(self) -> bool:
        return self.pass_win_rate and self.pass_expectancy and self.pass_drawdown

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["overall_pass"] = self.overall_pass
        return d


async def compute_paper_run_stats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    start_ist: date,
    end_ist: date,
    capital_inr: float,
) -> PaperRunStats:
    """Compute stats over the IST-date window [start_ist, end_ist] inclusive."""
    start_utc = datetime.combine(start_ist, time(0, 0), tzinfo=IST).astimezone(UTC)
    end_utc = datetime.combine(end_ist + timedelta(days=1), time(0, 0), tzinfo=IST).astimezone(UTC)

    async with session_factory() as session:
        positions = await _closed_positions(session, start_utc, end_utc)
        trades = await _enrich_with_signals(session, positions)
        fills_count, avg_slip, max_abs_slip = await _slippage(session, start_utc, end_utc)
        regime_total, regime_high_conf = await _regime_counts(session, start_utc, end_utc)
        regime_blocked = await _regime_block_count(session, start_utc, end_utc)

    return _aggregate(
        start_ist=start_ist,
        end_ist=end_ist,
        trades=trades,
        capital_inr=capital_inr,
        fills_count=fills_count,
        avg_slip=avg_slip,
        max_abs_slip=max_abs_slip,
        regime_total=regime_total,
        regime_high_conf=regime_high_conf,
        regime_blocked=regime_blocked,
    )


def _aggregate(
    *,
    start_ist: date,
    end_ist: date,
    trades: list[TradeRow],
    capital_inr: float,
    fills_count: int,
    avg_slip: float | None,
    max_abs_slip: float | None,
    regime_total: int,
    regime_high_conf: int,
    regime_blocked: int,
) -> PaperRunStats:
    trading_days = _count_trading_days(start_ist, end_ist)

    winners = [t for t in trades if t.realised_pnl > 0]
    losers = [t for t in trades if t.realised_pnl < 0]
    breakevens = [t for t in trades if t.realised_pnl == 0]

    total_pnl = sum(t.realised_pnl for t in trades)
    avg_winner = (sum(t.realised_pnl for t in winners) / len(winners)) if winners else None
    avg_loser = (sum(t.realised_pnl for t in losers) / len(losers)) if losers else None
    largest_winner = max((t.realised_pnl for t in winners), default=None)
    largest_loser = min((t.realised_pnl for t in losers), default=None)
    expectancy = (total_pnl / len(trades)) if trades else None
    win_rate = (100.0 * len(winners) / len(trades)) if trades else None
    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]
    avg_r = (sum(r_values) / len(r_values)) if r_values else None

    max_dd_inr = _max_drawdown_inr(trades)
    max_dd_pct = 100.0 * max_dd_inr / capital_inr if capital_inr > 0 else 0.0

    pass_win_rate = win_rate is not None and win_rate >= WIN_RATE_THRESHOLD_PCT
    pass_expectancy = expectancy is not None and expectancy > EXPECTANCY_THRESHOLD_INR
    pass_drawdown = max_dd_pct <= MAX_DRAWDOWN_THRESHOLD_PCT

    return PaperRunStats(
        period_start_ist=start_ist.isoformat(),
        period_end_ist=end_ist.isoformat(),
        trading_days=trading_days,
        total_trades=len(trades),
        wins=len(winners),
        losses=len(losers),
        breakevens=len(breakevens),
        total_pnl_inr=round(total_pnl, 2),
        avg_winner_inr=round(avg_winner, 2) if avg_winner is not None else None,
        avg_loser_inr=round(avg_loser, 2) if avg_loser is not None else None,
        largest_winner_inr=round(largest_winner, 2) if largest_winner is not None else None,
        largest_loser_inr=round(largest_loser, 2) if largest_loser is not None else None,
        expectancy_per_trade_inr=round(expectancy, 2) if expectancy is not None else None,
        win_rate_pct=round(win_rate, 2) if win_rate is not None else None,
        avg_r_multiple=round(avg_r, 3) if avg_r is not None else None,
        max_drawdown_inr=round(max_dd_inr, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        fills_count=fills_count,
        avg_slippage_bps=round(avg_slip, 2) if avg_slip is not None else None,
        max_abs_slippage_bps=round(max_abs_slip, 2) if max_abs_slip is not None else None,
        regime_calls=regime_total,
        risk_off_high_confidence_calls=regime_high_conf,
        signals_blocked_by_regime=regime_blocked,
        trades=trades,
        pass_win_rate=pass_win_rate,
        pass_expectancy=pass_expectancy,
        pass_drawdown=pass_drawdown,
    )


def _max_drawdown_inr(trades: list[TradeRow]) -> float:
    """Peak-to-trough drawdown on the realised-P&L equity curve."""
    if not trades:
        return 0.0
    equity = 0.0
    peak = 0.0
    worst = 0.0
    # Trades are already ordered by closed_at ascending in the query.
    for t in trades:
        equity += t.realised_pnl
        peak = max(peak, equity)
        dd = peak - equity
        worst = max(worst, dd)
    return worst


def _count_trading_days(start_ist: date, end_ist: date) -> int:
    from app.util.calendar import is_trading_day

    count = 0
    d = start_ist
    while d <= end_ist:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count


# ----- DB helpers ---------------------------------------------------------------


async def _closed_positions(
    session: AsyncSession, start_utc: datetime, end_utc: datetime
) -> list[m.Position]:
    rows = (
        (
            await session.execute(
                select(m.Position)
                .where(m.Position.closed_at.is_not(None))
                .where(m.Position.closed_at >= start_utc)
                .where(m.Position.closed_at < end_utc)
                .order_by(m.Position.closed_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _enrich_with_signals(
    session: AsyncSession, positions: list[m.Position]
) -> list[TradeRow]:
    """For each closed position, look up the originating signal to get the stop."""
    out: list[TradeRow] = []
    for p in positions:
        # The entry signal is the most recent non-BLOCKED Signal for the symbol
        # that fired at or before the position opened.
        sig = (
            await session.execute(
                select(m.Signal)
                .where(m.Signal.symbol == p.symbol)
                .where(m.Signal.created_at <= p.opened_at)
                .where(m.Signal.status != "BLOCKED")
                .order_by(m.Signal.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        risk_amount: float | None = None
        r_mult: float | None = None
        if sig is not None:
            stop_distance = abs(sig.breakout_price - sig.stop)
            if stop_distance > 0:
                risk_amount = stop_distance * abs(p.qty)
                if risk_amount > 0:
                    r_mult = float(p.realised_pnl) / risk_amount
        out.append(
            TradeRow(
                position_id=p.id,
                symbol=p.symbol,
                direction="long" if p.qty > 0 else "short",
                qty=abs(p.qty),
                entry_price=round(p.avg_entry, 2),
                closed_at_ist=p.closed_at.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
                if p.closed_at is not None
                else "",
                realised_pnl=round(float(p.realised_pnl or 0.0), 2),
                risk_amount_inr=round(risk_amount, 2) if risk_amount is not None else None,
                r_multiple=round(r_mult, 3) if r_mult is not None else None,
            )
        )
    return out


async def _slippage(
    session: AsyncSession, start_utc: datetime, end_utc: datetime
) -> tuple[int, float | None, float | None]:
    row = (
        await session.execute(
            select(
                func.count(m.SlippageLog.id),
                func.avg(m.SlippageLog.slippage_bps),
                func.max(func.abs(m.SlippageLog.slippage_bps)),
            )
            .where(m.SlippageLog.ts >= start_utc)
            .where(m.SlippageLog.ts < end_utc)
        )
    ).one()
    count, avg, mx = row
    return (
        int(count or 0),
        float(avg) if avg is not None else None,
        float(mx) if mx is not None else None,
    )


async def _regime_counts(
    session: AsyncSession, start_utc: datetime, end_utc: datetime
) -> tuple[int, int]:
    from app.risk.limits import REGIME_RISK_OFF_CONFIDENCE_THRESHOLD

    total = (
        await session.execute(
            select(func.count(m.RegimeVerdict.id))
            .where(m.RegimeVerdict.ts >= start_utc)
            .where(m.RegimeVerdict.ts < end_utc)
        )
    ).scalar_one()
    high = (
        await session.execute(
            select(func.count(m.RegimeVerdict.id))
            .where(m.RegimeVerdict.ts >= start_utc)
            .where(m.RegimeVerdict.ts < end_utc)
            .where(m.RegimeVerdict.regime == "risk_off")
            .where(m.RegimeVerdict.confidence > REGIME_RISK_OFF_CONFIDENCE_THRESHOLD)
        )
    ).scalar_one()
    return int(total), int(high)


async def _regime_block_count(session: AsyncSession, start_utc: datetime, end_utc: datetime) -> int:
    count = (
        await session.execute(
            select(func.count(m.RiskBlock.id))
            .where(m.RiskBlock.ts >= start_utc)
            .where(m.RiskBlock.ts < end_utc)
            .where(m.RiskBlock.reason == "regime_risk_off")
        )
    ).scalar_one()
    return int(count)


def format_text(stats: PaperRunStats) -> str:
    L: list[str] = []
    L.append(f"# Paper-to-Live Check — {stats.period_start_ist} → {stats.period_end_ist}")
    L.append("")
    L.append(f"Trading days in window: {stats.trading_days}")
    L.append("")
    L.append("## Trades")
    L.append(f"  Total      : {stats.total_trades}")
    L.append(f"  Wins       : {stats.wins}")
    L.append(f"  Losses     : {stats.losses}")
    L.append(f"  Break-evens: {stats.breakevens}")
    if stats.win_rate_pct is not None:
        L.append(f"  Win rate   : {stats.win_rate_pct:.1f}%")
    L.append("")
    L.append("## P&L (₹, net of charges)")
    L.append(f"  Total P&L           : {stats.total_pnl_inr:,.2f}")
    if stats.expectancy_per_trade_inr is not None:
        L.append(f"  Expectancy / trade  : {stats.expectancy_per_trade_inr:,.2f}")
    if stats.avg_winner_inr is not None:
        L.append(f"  Avg winner          : {stats.avg_winner_inr:,.2f}")
    if stats.avg_loser_inr is not None:
        L.append(f"  Avg loser           : {stats.avg_loser_inr:,.2f}")
    if stats.largest_winner_inr is not None:
        L.append(f"  Largest winner      : {stats.largest_winner_inr:,.2f}")
    if stats.largest_loser_inr is not None:
        L.append(f"  Largest loser       : {stats.largest_loser_inr:,.2f}")
    if stats.avg_r_multiple is not None:
        L.append(f"  Avg R-multiple      : {stats.avg_r_multiple:.2f} R")
    L.append("")
    L.append("## Drawdown")
    L.append(
        f"  Max drawdown        : ₹{stats.max_drawdown_inr:,.2f} "
        f"({stats.max_drawdown_pct:.2f}% of capital)"
    )
    L.append("")
    L.append("## Slippage")
    L.append(f"  Fills counted       : {stats.fills_count}")
    if stats.avg_slippage_bps is not None:
        L.append(f"  Avg slippage        : {stats.avg_slippage_bps:.2f} bps")
    if stats.max_abs_slippage_bps is not None:
        L.append(f"  Max |slippage|      : {stats.max_abs_slippage_bps:.2f} bps")
    L.append("")
    L.append("## Regime gating")
    L.append(f"  Regime calls            : {stats.regime_calls}")
    L.append(f"  risk_off > 0.7 conf     : {stats.risk_off_high_confidence_calls}")
    L.append(f"  Signals blocked by regime: {stats.signals_blocked_by_regime}")
    L.append("")
    L.append("## Gates")
    L.append(
        f"  Win rate ≥ {WIN_RATE_THRESHOLD_PCT}%      : {'PASS' if stats.pass_win_rate else 'FAIL'}"
    )
    L.append(
        f"  Expectancy > {EXPECTANCY_THRESHOLD_INR}      : "
        f"{'PASS' if stats.pass_expectancy else 'FAIL'}"
    )
    L.append(
        f"  Max drawdown ≤ {MAX_DRAWDOWN_THRESHOLD_PCT}% : "
        f"{'PASS' if stats.pass_drawdown else 'FAIL'}"
    )
    L.append("")
    verdict = (
        "GO — graduate to live (start at ₹10k)" if stats.overall_pass else "NO-GO — stay in paper"
    )
    L.append(f"## Verdict: {verdict}")
    return "\n".join(L)
