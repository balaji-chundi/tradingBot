from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.brokers.base import NewOrder
from app.brokers.paper import PaperBroker
from app.data.types import Tick
from app.journal.db import get_session_factory, init_db
from app.journal.models import Fill, Order, SlippageLog


def _tick(symbol: str, ltp: float) -> Tick:
    return Tick(
        symbol=symbol, token="2885", ltp=ltp, ltq=1, total_volume=1000, ts=datetime.now(UTC)
    )


@pytest.mark.asyncio
async def test_place_order_is_pending_until_next_tick() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf)
    order_id = await broker.place_order(
        NewOrder(symbol="RELIANCE-EQ", side="BUY", qty=4, ideal_price=2500.0)
    )
    async with sf() as session:
        row = await session.get(Order, order_id)
        assert row is not None
        assert row.status == "PENDING"
        assert row.broker_order_id is None
    assert broker.has_pending_orders()


@pytest.mark.asyncio
async def test_buy_fill_applies_positive_slippage() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf, slippage_bps=5.0)
    order_id = await broker.place_order(
        NewOrder(symbol="RELIANCE-EQ", side="BUY", qty=4, ideal_price=2500.0)
    )
    fills = await broker.on_tick(_tick("RELIANCE-EQ", 2500.0))
    assert len(fills) == 1
    f = fills[0]
    assert f.order_id == order_id
    # 5 bps positive slippage → 2500 * 1.0005 = 2501.25
    assert f.price == pytest.approx(2501.25)
    assert not broker.has_pending_orders()


@pytest.mark.asyncio
async def test_sell_fill_applies_negative_slippage() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf, slippage_bps=5.0)
    await broker.place_order(NewOrder(symbol="RELIANCE-EQ", side="SELL", qty=4, ideal_price=2500.0))
    fills = await broker.on_tick(_tick("RELIANCE-EQ", 2500.0))
    f = fills[0]
    assert f.price == pytest.approx(2498.75)  # 2500 * 0.9995


@pytest.mark.asyncio
async def test_fill_writes_order_fill_and_slippage_rows() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf, slippage_bps=5.0)
    order_id = await broker.place_order(
        NewOrder(symbol="RELIANCE-EQ", side="BUY", qty=4, ideal_price=2500.0)
    )
    await broker.on_tick(_tick("RELIANCE-EQ", 2500.0))

    async with sf() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == "FILLED"
        assert order.broker_order_id == f"PAPER-{order_id}"

        fill_row = (
            await session.execute(select(Fill).where(Fill.order_id == order_id))
        ).scalar_one()
        assert fill_row.qty == 4
        assert fill_row.charges_inr > 0

        slip_row = (
            await session.execute(select(SlippageLog).where(SlippageLog.order_id == order_id))
        ).scalar_one()
        assert slip_row.ideal_price == 2500.0
        assert slip_row.slippage_bps == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_orders_for_other_symbols_dont_fill() -> None:
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf)
    await broker.place_order(NewOrder(symbol="RELIANCE-EQ", side="BUY", qty=1))
    fills = await broker.on_tick(_tick("HDFCBANK-EQ", 1600.0))
    assert fills == []
    assert broker.has_pending_orders()
