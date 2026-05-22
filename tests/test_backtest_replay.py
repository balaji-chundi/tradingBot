"""Replay engine tests — synthesize 1-min bars in memory and assert ORB
signal generation + simulated exits produce the expected trade rows.

The replay engine is the heaviest piece of code in the backtest stack and
the one most likely to drift from the live engine's semantics over time.
These tests pin down the contract: same OR window math, same sizing math,
same exit precedence (kill / time-stop / stop / target), same re-entry
block on stop-outs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.backtest.replay import run_backtest
from app.config import IST
from app.data.types import Bar

DAY = date(2026, 5, 20)


def _bar(
    symbol: str,
    h_ist: int,
    m_ist: int,
    *,
    o: float,
    high: float,
    low: float,
    c: float,
    v: int,
    day: date = DAY,
) -> Bar:
    open_ist = datetime(day.year, day.month, day.day, h_ist, m_ist, tzinfo=IST)
    open_utc = open_ist.astimezone(UTC)
    return Bar(
        symbol=symbol,
        open_time=open_utc,
        close_time=open_utc + timedelta(minutes=1),
        open=o,
        high=high,
        low=low,
        close=c,
        volume=v,
    )


def _build_or_window(symbol: str, or_high: float, or_low: float, *, vol: int = 1000) -> list[Bar]:
    """15 OR bars 09:15..09:29 with the first one defining the high/low."""
    bars = []
    for i in range(15):
        if i == 0:
            h, low_ = or_high, or_low
        else:
            h, low_ = or_high - 0.5, or_low + 0.5
        bars.append(
            _bar(
                symbol,
                9,
                15 + i,
                o=(or_high + or_low) / 2,
                high=h,
                low=low_,
                c=(or_high + or_low) / 2,
                v=vol,
            )
        )
    return bars


@pytest.mark.asyncio
async def test_empty_universe_produces_empty_result() -> None:
    result = run_backtest({DAY: {}}, capital_inr=50_000.0)
    assert result.trades == []
    assert len(result.sessions) == 1
    assert result.sessions[0].signals_fired == 0


@pytest.mark.asyncio
async def test_clean_long_target_hit() -> None:
    """ORB long fires; next bar fills entry; later bar hits target."""
    bars = _build_or_window("HDFCBANK-EQ", or_high=700.0, or_low=680.0)
    # 09:30 breakout bar — close above OR-high with 2x volume
    bars.append(_bar("HDFCBANK-EQ", 9, 30, o=700.5, high=702.0, low=700.0, c=701.5, v=2000))
    # 09:31 — fills entry at this bar's open (~701.5 area, with slip)
    bars.append(_bar("HDFCBANK-EQ", 9, 31, o=702.0, high=705.0, low=701.0, c=704.0, v=1500))
    # 09:32 — target = 701.5 + 1.5 * (701.5 - 680) = 733.75. We'll fast-forward
    # with bars in-range first, then a bar with high >= target.
    bars.append(_bar("HDFCBANK-EQ", 9, 32, o=704.0, high=735.0, low=703.0, c=734.0, v=2000))

    result = run_backtest({DAY: {"HDFCBANK-EQ": bars}}, capital_inr=50_000.0)
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.symbol == "HDFCBANK-EQ"
    assert t.direction == "long"
    assert t.exit_reason == "target_hit"
    assert t.net_pnl > 0
    assert t.gross_pnl > t.net_pnl  # charges ate some
    # R-multiple roughly +1.5 (target is 1.5R distance away)
    assert t.r_multiple == pytest.approx(1.5, abs=0.5)


@pytest.mark.asyncio
async def test_stop_hit_blocks_reentry() -> None:
    """First signal stops out; even if a second breakout fires, it must be blocked."""
    bars = _build_or_window("HDFCBANK-EQ", or_high=700.0, or_low=680.0)
    # First breakout
    bars.append(_bar("HDFCBANK-EQ", 9, 30, o=700.5, high=702.0, low=700.0, c=701.5, v=2000))
    # Entry fills at 9:31, then stop hits in 9:32 (bar.low <= 680)
    bars.append(_bar("HDFCBANK-EQ", 9, 31, o=702.0, high=702.5, low=679.0, c=679.5, v=2500))
    # Now another breakout candidate — but should be blocked by stopped_out_today
    bars.append(_bar("HDFCBANK-EQ", 9, 32, o=679.5, high=702.5, low=679.0, c=702.4, v=2200))

    result = run_backtest({DAY: {"HDFCBANK-EQ": bars}}, capital_inr=50_000.0)
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_hit"
    assert result.trades[0].net_pnl < 0


@pytest.mark.asyncio
async def test_time_stop_at_1515_ist() -> None:
    bars = _build_or_window("HDFCBANK-EQ", or_high=700.0, or_low=680.0)
    bars.append(_bar("HDFCBANK-EQ", 9, 30, o=700.5, high=702.0, low=700.0, c=701.5, v=2000))
    # Entry fills at 9:31; price oscillates between stop/target for hours
    bars.append(_bar("HDFCBANK-EQ", 9, 31, o=702.0, high=705.0, low=698.0, c=703.0, v=1500))
    # Skip ahead — add a bar at 15:14 still in-range, then the 15:15 bar fires time-stop
    bars.append(_bar("HDFCBANK-EQ", 15, 14, o=702.0, high=703.5, low=699.0, c=701.0, v=1200))
    bars.append(_bar("HDFCBANK-EQ", 15, 15, o=701.5, high=702.0, low=700.5, c=701.8, v=1100))

    result = run_backtest({DAY: {"HDFCBANK-EQ": bars}}, capital_inr=50_000.0)
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "time_stop"


@pytest.mark.asyncio
async def test_sizing_block_doesnt_consume_trade_slot() -> None:
    """High-priced stock with tight stop → notional > 0.9 × cap → sizing rejects."""
    # TCS at ~2300 with stop only ~₹3 below: qty = 500/3 = 166; notional = ~382k > 45k cap
    bars = _build_or_window("TCS-EQ", or_high=2300.0, or_low=2297.0, vol=1000)
    bars.append(_bar("TCS-EQ", 9, 30, o=2300.5, high=2302.0, low=2299.5, c=2301.5, v=2000))
    bars.append(_bar("TCS-EQ", 9, 31, o=2302.0, high=2303.0, low=2301.0, c=2302.5, v=1500))

    result = run_backtest({DAY: {"TCS-EQ": bars}}, capital_inr=50_000.0)
    assert len(result.trades) == 0
    assert result.sessions[0].signals_fired == 1
    assert result.sessions[0].signals_blocked_by_sizing == 1
    assert result.sessions[0].trades_taken == 0
