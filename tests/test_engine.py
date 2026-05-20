"""Integration tests for ExecutionEngine ↔ PaperBroker end-to-end.

These tests use the real PaperBroker, so they exercise the full pipeline
(signal → risk → sizing → order → fill → position; tick → exit triggers).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.brokers.paper import PaperBroker
from app.config import IST
from app.data.types import Signal, Tick
from app.execution.engine import ExecutionEngine
from app.journal.db import get_session_factory, init_db
from app.journal.models import Order, Position
from app.journal.models import Signal as SignalRow


def _ist(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 5, 20, h, m, s, tzinfo=IST).astimezone(UTC)


def _signal(
    *,
    symbol: str = "HDFCBANK-EQ",
    direction: str = "long",
    entry: float = 700.0,
    stop: float = 680.0,
    target: float | None = None,
    ts_ist: tuple[int, int] = (9, 30),
) -> Signal:
    if target is None:
        if direction == "long":
            target = entry + 1.5 * (entry - stop)
        else:
            target = entry - 1.5 * (stop - entry)
    return Signal(
        symbol=symbol,
        direction=direction,
        breakout_close_time=_ist(*ts_ist),
        breakout_price=entry,
        or_high=entry if direction == "long" else stop,
        or_low=stop if direction == "long" else entry,
        stop=stop,
        target=target,
        bar_volume=10_000,
        avg_prior_5bar_volume=5_000,
        volume_ratio=2.0,
    )


def _tick(symbol: str, ltp: float, ts: datetime | None = None) -> Tick:
    return Tick(
        symbol=symbol,
        token="1333",
        ltp=ltp,
        ltq=10,
        total_volume=100_000,
        ts=ts or _ist(10, 0),
    )


@pytest.mark.asyncio
async def test_full_long_lifecycle_target_hit() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf, slippage_bps=5.0)
    engine = ExecutionEngine(broker, sf)

    sig = _signal(symbol="HDFCBANK-EQ", direction="long", entry=700.0, stop=680.0)
    signal_id = await engine.on_signal(sig)
    assert signal_id is not None

    # Tick 1: fills the entry BUY at LTP=700 → 700.35 with 5 bps slippage.
    await engine.on_tick(_tick("HDFCBANK-EQ", 700.0, _ist(10, 0)))
    # Tick 2: LTP at target (700 + 1.5×20 = 730). Exit SELL queued for next tick.
    await engine.on_tick(_tick("HDFCBANK-EQ", 730.0, _ist(10, 5)))
    # Tick 3: fills the exit SELL.
    await engine.on_tick(_tick("HDFCBANK-EQ", 730.0, _ist(10, 5, 1)))

    async with sf() as session:
        pos = (await session.execute(select(Position))).scalar_one()
        sig_row = await session.get(SignalRow, signal_id)
    assert pos.closed_at is not None
    assert pos.realised_pnl > 0  # target was above entry; should be profitable
    assert sig_row is not None
    assert sig_row.status == "FILLED"


@pytest.mark.asyncio
async def test_full_long_lifecycle_stop_hit_records_stopout() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf, slippage_bps=5.0)
    engine = ExecutionEngine(broker, sf)

    sig = _signal(symbol="HDFCBANK-EQ", direction="long", entry=700.0, stop=680.0)
    await engine.on_signal(sig)
    await engine.on_tick(_tick("HDFCBANK-EQ", 700.0, _ist(10, 0)))
    # Stop hit at LTP=680
    await engine.on_tick(_tick("HDFCBANK-EQ", 680.0, _ist(10, 5)))
    await engine.on_tick(_tick("HDFCBANK-EQ", 680.0, _ist(10, 5, 1)))

    async with sf() as session:
        pos = (await session.execute(select(Position))).scalar_one()
    assert pos.closed_at is not None
    assert pos.realised_pnl < 0  # stopped out → loss
    assert "HDFCBANK-EQ" in engine._stopped_out_today  # noqa: SLF001


@pytest.mark.asyncio
async def test_short_lifecycle_target_hit() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf, slippage_bps=5.0)
    engine = ExecutionEngine(broker, sf)

    # Short: entry=700, stop=720, target=700-1.5*20=670
    sig = _signal(symbol="HDFCBANK-EQ", direction="short", entry=700.0, stop=720.0)
    await engine.on_signal(sig)
    await engine.on_tick(_tick("HDFCBANK-EQ", 700.0, _ist(10, 0)))  # entry fill (SELL)
    await engine.on_tick(_tick("HDFCBANK-EQ", 670.0, _ist(10, 5)))  # target → BUY queued
    await engine.on_tick(_tick("HDFCBANK-EQ", 670.0, _ist(10, 5, 1)))  # exit BUY fills

    async with sf() as session:
        pos = (await session.execute(select(Position))).scalar_one()
    assert pos.closed_at is not None
    assert pos.realised_pnl > 0


@pytest.mark.asyncio
async def test_time_stop_fires_after_1515_ist() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf, slippage_bps=5.0)
    engine = ExecutionEngine(broker, sf)

    sig = _signal(symbol="HDFCBANK-EQ", direction="long", entry=700.0, stop=680.0)
    await engine.on_signal(sig)
    await engine.on_tick(_tick("HDFCBANK-EQ", 700.0, _ist(10, 0)))
    # Neither stop nor target hit yet
    await engine.on_tick(_tick("HDFCBANK-EQ", 705.0, _ist(15, 14, 59)))
    # 15:15 IST → time-stop fires; exit order placed
    await engine.on_tick(_tick("HDFCBANK-EQ", 705.0, _ist(15, 15, 0)))
    # Next tick fills it
    await engine.on_tick(_tick("HDFCBANK-EQ", 705.0, _ist(15, 15, 1)))

    async with sf() as session:
        orders = (await session.execute(select(Order))).scalars().all()
        pos = (await session.execute(select(Position))).scalar_one()
    # Two orders: entry BUY + exit SELL
    assert len(orders) == 2
    assert any(o.payload and o.payload.get("role") == "time_stop" for o in orders)
    assert pos.closed_at is not None


@pytest.mark.asyncio
async def test_risk_block_outside_window_persists_block_row_and_no_order() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf)
    engine = ExecutionEngine(broker, sf)

    sig = _signal(symbol="HDFCBANK-EQ", entry=700.0, stop=680.0, ts_ist=(9, 25))
    result = await engine.on_signal(sig)
    assert result is None

    async with sf() as session:
        orders = (await session.execute(select(Order))).scalars().all()
        signals = (await session.execute(select(SignalRow))).scalars().all()
    assert orders == []  # No order placed
    assert signals == []  # And no Signal persisted either
    assert not broker.has_pending_orders()


@pytest.mark.asyncio
async def test_max_two_trades_per_day() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf, slippage_bps=5.0)
    engine = ExecutionEngine(broker, sf)

    # First trade: HDFCBANK long
    s1 = _signal(symbol="HDFCBANK-EQ", entry=700.0, stop=680.0, ts_ist=(9, 30))
    assert await engine.on_signal(s1) is not None
    await engine.on_tick(_tick("HDFCBANK-EQ", 700.0, _ist(10, 0)))
    # Close it via target so we don't hit "max open positions" first
    await engine.on_tick(_tick("HDFCBANK-EQ", 730.0, _ist(10, 5)))
    await engine.on_tick(_tick("HDFCBANK-EQ", 730.0, _ist(10, 5, 1)))

    # Second trade: ICICIBANK long
    s2 = _signal(symbol="ICICIBANK-EQ", entry=1200.0, stop=1180.0, ts_ist=(10, 10))
    assert await engine.on_signal(s2) is not None

    # Third signal — should be blocked by max_trades_per_day_reached
    s3 = _signal(symbol="INFY-EQ", entry=1200.0, stop=1180.0, ts_ist=(11, 0))
    result = await engine.on_signal(s3)
    assert result is None


@pytest.mark.asyncio
async def test_sizing_block_when_notional_exceeds_cap() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf)
    engine = ExecutionEngine(broker, sf)

    # Tight stop on a high-priced stock → qty*entry blows past 0.9 × 50k = 45k.
    sig = _signal(symbol="TCS-EQ", entry=2300.0, stop=2280.0, ts_ist=(9, 30))
    result = await engine.on_signal(sig)
    assert result is None
    assert not broker.has_pending_orders()


@pytest.mark.asyncio
async def test_pnl_math_matches_round_trip_after_charges() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf, slippage_bps=5.0)
    engine = ExecutionEngine(broker, sf)

    sig = _signal(symbol="HDFCBANK-EQ", direction="long", entry=700.0, stop=680.0)
    await engine.on_signal(sig)
    await engine.on_tick(_tick("HDFCBANK-EQ", 700.0, _ist(10, 0)))
    await engine.on_tick(_tick("HDFCBANK-EQ", 730.0, _ist(10, 5)))
    await engine.on_tick(_tick("HDFCBANK-EQ", 730.0, _ist(10, 5, 1)))

    async with sf() as session:
        pos = (await session.execute(select(Position))).scalar_one()
    # qty=25 (risk 500 / stop dist 20).
    # Entry fill ≈ 700 × 1.0005 = 700.35; exit fill ≈ 730 × 0.9995 = 729.635
    # Gross ≈ (729.635 - 700.35) × 25 ≈ 732.13
    # Charges per round-trip ≈ ₹50-60 typical
    # Net ≈ 670-680
    assert pos.qty == 25
    assert 600.0 < pos.realised_pnl < 720.0
