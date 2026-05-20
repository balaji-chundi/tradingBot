from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.journal.db import get_session_factory, init_db
from app.journal.models import LLMCall, RegimeVerdict, Signal, Tick


@pytest.mark.asyncio
async def test_tick_roundtrip() -> None:
    await init_db()
    session_factory = get_session_factory()
    async with session_factory() as session:
        session.add(
            Tick(
                symbol="RELIANCE-EQ",
                ltp=2500.25,
                ltq=10,
                total_volume=1_000_000,
                ts=datetime.now(UTC),
                raw={"vendor": "test", "seq": 1},
            )
        )
        await session.commit()

        result = await session.execute(select(Tick).where(Tick.symbol == "RELIANCE-EQ"))
        row = result.scalar_one()
        assert row.ltp == 2500.25
        assert row.raw == {"vendor": "test", "seq": 1}
        assert row.ts.tzinfo is not None


@pytest.mark.asyncio
async def test_signal_roundtrip() -> None:
    await init_db()
    session_factory = get_session_factory()
    async with session_factory() as session:
        session.add(
            Signal(
                symbol="HDFCBANK-EQ",
                direction="long",
                breakout_price=1600.0,
                or_high=1599.5,
                or_low=1585.0,
                qty=3,
                stop=1585.0,
                target=1621.75,
                status="NEW",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
        result = await session.execute(select(Signal))
        row = result.scalar_one()
        assert row.direction == "long"
        assert row.status == "NEW"


@pytest.mark.asyncio
async def test_llm_call_and_regime_link() -> None:
    await init_db()
    session_factory = get_session_factory()
    async with session_factory() as session:
        call = LLMCall(
            ts=datetime.now(UTC),
            tier="regime",
            model="gemini-2.5-pro",
            prompt_version="v1",
            prompt="...",
            response='{"regime":"neutral"}',
            latency_ms=820,
            tokens_in=1200,
            tokens_out=180,
        )
        session.add(call)
        await session.flush()
        session.add(
            RegimeVerdict(
                ts=datetime.now(UTC),
                regime="neutral",
                confidence=0.6,
                key_drivers=["flat breadth", "VIX unchanged"],
                watch_symbols=["RELIANCE"],
                avoid_symbols=[],
                rationale="No conviction either way.",
                llm_call_id=call.id,
            )
        )
        await session.commit()

        verdict = (await session.execute(select(RegimeVerdict))).scalar_one()
        assert verdict.regime == "neutral"
        assert verdict.key_drivers == ["flat breadth", "VIX unchanged"]
        assert verdict.llm_call_id == call.id
