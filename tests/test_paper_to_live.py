"""Phase 7 paper-run analyzer tests.

Seed positions/signals/slippage/regime rows and assert the computed stats +
gate verdicts. Covers: empty DB, mixed wins/losses, max drawdown math, gate
thresholds (each gate trips on its own without affecting the others).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.analytics.paper_run import (
    EXPECTANCY_THRESHOLD_INR,
    MAX_DRAWDOWN_THRESHOLD_PCT,
    WIN_RATE_THRESHOLD_PCT,
    compute_paper_run_stats,
    format_text,
)
from app.config import IST
from app.journal import models as m
from app.journal.db import get_session_factory, init_db

CAPITAL = 50_000.0


def _ist(y: int, mo: int, d: int, h: int = 10, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=IST).astimezone(UTC)


async def _seed_trade(
    sf: object,
    *,
    symbol: str,
    direction: str,
    realised_pnl: float,
    closed_at: datetime,
    qty: int = 10,
    entry: float = 700.0,
    stop: float = 680.0,
) -> None:
    async with sf() as session:  # type: ignore[operator]
        # The entry Signal — used by the analyzer to compute R-multiple.
        sig = m.Signal(
            symbol=symbol,
            direction=direction,
            breakout_price=entry,
            or_high=entry if direction == "long" else stop,
            or_low=stop if direction == "long" else entry,
            qty=qty,
            stop=stop,
            target=entry + 1.5 * (entry - stop)
            if direction == "long"
            else entry - 1.5 * (stop - entry),
            status="FILLED",
            created_at=closed_at - timedelta(minutes=30),
        )
        session.add(sig)
        session.add(
            m.Position(
                symbol=symbol,
                qty=qty if direction == "long" else -qty,
                avg_entry=entry,
                opened_at=closed_at - timedelta(minutes=20),
                closed_at=closed_at,
                realised_pnl=realised_pnl,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_empty_db_produces_no_trades_no_gates_pass() -> None:
    await init_db()
    sf = get_session_factory()
    stats = await compute_paper_run_stats(
        sf, start_ist=date(2026, 5, 21), end_ist=date(2026, 6, 3), capital_inr=CAPITAL
    )
    assert stats.total_trades == 0
    assert stats.win_rate_pct is None
    assert stats.expectancy_per_trade_inr is None
    assert stats.pass_win_rate is False
    assert stats.pass_expectancy is False
    assert stats.pass_drawdown is True  # no trades → no drawdown
    assert stats.overall_pass is False


@pytest.mark.asyncio
async def test_passing_run_meets_all_gates() -> None:
    """4 wins of ₹400 each, 2 losses of ₹150 each → win rate 66.7%, +₹1300 net,
    drawdown well below 8% of 50k."""
    await init_db()
    sf = get_session_factory()
    base = _ist(2026, 5, 25, 10, 0)
    for i, pnl in enumerate([400, 400, -150, 400, -150, 400]):
        await _seed_trade(
            sf,
            symbol="HDFCBANK-EQ",
            direction="long",
            realised_pnl=pnl,
            closed_at=base + timedelta(hours=i),
        )

    stats = await compute_paper_run_stats(
        sf, start_ist=date(2026, 5, 25), end_ist=date(2026, 5, 25), capital_inr=CAPITAL
    )
    assert stats.total_trades == 6
    assert stats.wins == 4
    assert stats.losses == 2
    assert stats.win_rate_pct is not None and stats.win_rate_pct > WIN_RATE_THRESHOLD_PCT
    assert (
        stats.expectancy_per_trade_inr is not None
        and stats.expectancy_per_trade_inr > EXPECTANCY_THRESHOLD_INR
    )
    assert stats.max_drawdown_pct <= MAX_DRAWDOWN_THRESHOLD_PCT
    assert stats.pass_win_rate
    assert stats.pass_expectancy
    assert stats.pass_drawdown
    assert stats.overall_pass


@pytest.mark.asyncio
async def test_failing_win_rate_only() -> None:
    """3 wins ₹100 each, 7 losses ₹40 each → 30% win rate (fail), but +₹20 expectancy."""
    await init_db()
    sf = get_session_factory()
    base = _ist(2026, 5, 25, 10, 0)
    for i in range(3):
        await _seed_trade(
            sf,
            symbol="HDFCBANK-EQ",
            direction="long",
            realised_pnl=100.0,
            closed_at=base + timedelta(hours=i),
        )
    for i in range(7):
        await _seed_trade(
            sf,
            symbol="HDFCBANK-EQ",
            direction="long",
            realised_pnl=-40.0,
            closed_at=base + timedelta(hours=3 + i),
        )

    stats = await compute_paper_run_stats(
        sf, start_ist=date(2026, 5, 25), end_ist=date(2026, 5, 25), capital_inr=CAPITAL
    )
    assert stats.win_rate_pct == pytest.approx(30.0)
    assert stats.expectancy_per_trade_inr == pytest.approx(2.0)
    assert stats.pass_win_rate is False
    assert stats.pass_expectancy is True
    assert stats.overall_pass is False


@pytest.mark.asyncio
async def test_drawdown_math_uses_peak_to_trough() -> None:
    """Trades in order: +500, +500, -800, -300 → equity curve: 500, 1000, 200, -100.
    Peak=1000, lowest after=−100, drawdown = 1000-(-100) = 1100. capital=50k → 2.2%."""
    await init_db()
    sf = get_session_factory()
    base = _ist(2026, 5, 25, 10, 0)
    for i, pnl in enumerate([500, 500, -800, -300]):
        await _seed_trade(
            sf,
            symbol="HDFCBANK-EQ",
            direction="long",
            realised_pnl=pnl,
            closed_at=base + timedelta(hours=i),
        )
    stats = await compute_paper_run_stats(
        sf, start_ist=date(2026, 5, 25), end_ist=date(2026, 5, 25), capital_inr=CAPITAL
    )
    assert stats.max_drawdown_inr == pytest.approx(1100.0)
    assert stats.max_drawdown_pct == pytest.approx(2.2)


@pytest.mark.asyncio
async def test_drawdown_failure_blows_through_8pct() -> None:
    """One catastrophic loss of ₹5000 (10% of 50k) trips the drawdown gate."""
    await init_db()
    sf = get_session_factory()
    base = _ist(2026, 5, 25, 10, 0)
    for i, pnl in enumerate([200, -5000, 200, 200]):
        await _seed_trade(
            sf,
            symbol="HDFCBANK-EQ",
            direction="long",
            realised_pnl=pnl,
            closed_at=base + timedelta(hours=i),
        )
    stats = await compute_paper_run_stats(
        sf, start_ist=date(2026, 5, 25), end_ist=date(2026, 5, 25), capital_inr=CAPITAL
    )
    assert stats.max_drawdown_pct > MAX_DRAWDOWN_THRESHOLD_PCT
    assert stats.pass_drawdown is False


@pytest.mark.asyncio
async def test_r_multiple_uses_signal_stop_distance() -> None:
    """qty=10, entry=700, stop=680 → risk_amount = 200; realised +400 → R=2.0."""
    await init_db()
    sf = get_session_factory()
    base = _ist(2026, 5, 25, 10, 0)
    await _seed_trade(
        sf,
        symbol="HDFCBANK-EQ",
        direction="long",
        qty=10,
        entry=700.0,
        stop=680.0,
        realised_pnl=400.0,
        closed_at=base,
    )
    stats = await compute_paper_run_stats(
        sf, start_ist=date(2026, 5, 25), end_ist=date(2026, 5, 25), capital_inr=CAPITAL
    )
    assert stats.trades[0].risk_amount_inr == pytest.approx(200.0)
    assert stats.trades[0].r_multiple == pytest.approx(2.0)
    assert stats.avg_r_multiple == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_window_filters_by_ist_date() -> None:
    await init_db()
    sf = get_session_factory()
    inside = _ist(2026, 5, 25, 10, 0)
    outside_before = _ist(2026, 5, 24, 23, 0)
    outside_after = _ist(2026, 5, 27, 1, 0)
    for ts in (outside_before, inside, outside_after):
        await _seed_trade(
            sf,
            symbol="HDFCBANK-EQ",
            direction="long",
            realised_pnl=100.0,
            closed_at=ts,
        )
    stats = await compute_paper_run_stats(
        sf, start_ist=date(2026, 5, 25), end_ist=date(2026, 5, 25), capital_inr=CAPITAL
    )
    assert stats.total_trades == 1
    assert stats.trades[0].closed_at_ist.startswith("2026-05-25")


@pytest.mark.asyncio
async def test_format_text_renders_verdict_and_key_sections() -> None:
    await init_db()
    sf = get_session_factory()
    base = _ist(2026, 5, 25, 10, 0)
    await _seed_trade(
        sf, symbol="HDFCBANK-EQ", direction="long", realised_pnl=300.0, closed_at=base
    )
    stats = await compute_paper_run_stats(
        sf, start_ist=date(2026, 5, 25), end_ist=date(2026, 5, 25), capital_inr=CAPITAL
    )
    out = format_text(stats)
    assert "Paper-to-Live Check" in out
    assert "## Trades" in out
    assert "## P&L" in out
    assert "## Drawdown" in out
    assert "## Gates" in out
    assert "## Verdict:" in out
