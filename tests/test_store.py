from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.data.store import write_bar, write_ticks_batch
from app.data.types import Bar
from app.data.types import Tick as TickT
from app.journal.db import get_session_factory, init_db
from app.journal.models import Bar as BarRow
from app.journal.models import Tick as TickRow


@pytest.mark.asyncio
async def test_write_ticks_batch_roundtrip() -> None:
    await init_db()
    sf = get_session_factory()
    ts = datetime.now(UTC)
    batch = [
        TickT(
            symbol="RELIANCE-EQ",
            token="2885",
            ltp=2500.0 + i,
            ltq=10,
            total_volume=1000 + i * 5,
            ts=ts + timedelta(milliseconds=i * 100),
            raw={"i": i},
        )
        for i in range(50)
    ]
    n = await write_ticks_batch(batch, sf)
    assert n == 50

    async with sf() as session:
        result = await session.execute(select(TickRow).where(TickRow.symbol == "RELIANCE-EQ"))
        rows = result.scalars().all()
    assert len(rows) == 50
    assert rows[0].ts.tzinfo is not None
    assert rows[0].raw == {"i": 0}


@pytest.mark.asyncio
async def test_write_bar_roundtrip() -> None:
    await init_db()
    sf = get_session_factory()
    open_t = datetime(2026, 5, 20, 9, 15, 0, tzinfo=UTC)
    bar = Bar(
        symbol="RELIANCE-EQ",
        open_time=open_t,
        close_time=open_t + timedelta(minutes=1),
        open=2500.0,
        high=2510.0,
        low=2498.0,
        close=2505.0,
        volume=12345,
    )
    await write_bar(bar, sf)

    async with sf() as session:
        result = await session.execute(select(BarRow).where(BarRow.symbol == "RELIANCE-EQ"))
        row = result.scalar_one()
    assert row.o == 2500.0
    assert row.h == 2510.0
    assert row.low == 2498.0
    assert row.c == 2505.0
    assert row.v == 12345
    assert row.open_time.tzinfo is not None
