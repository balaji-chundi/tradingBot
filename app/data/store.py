"""Tick + bar persistence helpers.

Functions are async and take a session factory so callers control transaction
scope. Ticks are written in batches by the orchestrator to amortize SQLite
write overhead; bars are written one at a time (low frequency).
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.data.types import Bar, Tick
from app.journal import models as m

log = structlog.get_logger()


async def write_ticks_batch(
    ticks: Sequence[Tick],
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Persist a batch of ticks. Returns the number of rows written."""
    if not ticks:
        return 0
    rows = [
        m.Tick(
            symbol=t.symbol,
            ltp=t.ltp,
            ltq=t.ltq,
            total_volume=t.total_volume,
            ts=t.ts,
            raw=t.raw,
        )
        for t in ticks
    ]
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()
    return len(rows)


async def write_bar(
    bar: Bar,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Persist a single closed 1-minute bar."""
    row = m.Bar(
        symbol=bar.symbol,
        interval=bar.interval,
        open_time=bar.open_time,
        close_time=bar.close_time,
        o=bar.open,
        h=bar.high,
        low=bar.low,
        c=bar.close,
        v=bar.volume,
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()
