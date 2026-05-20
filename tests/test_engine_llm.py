"""Engine ↔ LLM integration: regime-block + pretrade skip/reduce_size paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.brokers.paper import PaperBroker
from app.config import IST
from app.data.types import Signal, Tick
from app.execution.engine import ExecutionEngine
from app.journal import models as m
from app.journal.db import get_session_factory, init_db
from app.journal.models import Order
from app.journal.models import Signal as SignalRow
from app.llm.client import GeminiClient
from app.llm.schemas import PretradeDecision


class StubGeminiClient(GeminiClient):
    def __init__(self, sf: Any, response: BaseModel) -> None:
        super().__init__(sf)
        self._resp = response

    async def generate_json(self, **kwargs: Any) -> tuple[Any, int]:  # type: ignore[override]
        return self._resp, 999


def _ist(h: int, mi: int, s: int = 0) -> datetime:
    return datetime(2026, 5, 20, h, mi, s, tzinfo=IST).astimezone(UTC)


def _signal(
    symbol: str = "HDFCBANK-EQ",
    *,
    entry: float = 700.0,
    stop: float = 680.0,
    ts_ist: tuple[int, int] = (9, 31),
) -> Signal:
    target = entry + 1.5 * (entry - stop)
    return Signal(
        symbol=symbol,
        direction="long",
        breakout_close_time=_ist(*ts_ist),
        breakout_price=entry,
        or_high=entry,
        or_low=stop,
        stop=stop,
        target=target,
        bar_volume=10_000,
        avg_prior_5bar_volume=5_000,
        volume_ratio=2.0,
    )


async def _seed_regime(sf: Any, *, regime: str, confidence: float, ts: datetime) -> None:
    async with sf() as session:
        session.add(
            m.RegimeVerdict(
                ts=ts,
                regime=regime,
                confidence=confidence,
                key_drivers=["test"],
                watch_symbols=[],
                avoid_symbols=[],
                rationale="seeded for test",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_regime_risk_off_blocks_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRETRADE_LLM_CHECK", "false")
    monkeypatch.setenv("RESPECT_REGIME", "true")
    await init_db()
    sf = get_session_factory()
    sig = _signal(ts_ist=(9, 31))
    await _seed_regime(
        sf, regime="risk_off", confidence=0.85, ts=sig.breakout_close_time - timedelta(minutes=2)
    )

    broker = PaperBroker(sf)
    engine = ExecutionEngine(broker, sf)
    result = await engine.on_signal(sig)
    assert result is None

    async with sf() as session:
        signals = (await session.execute(select(SignalRow))).scalars().all()
        orders = (await session.execute(select(Order))).scalars().all()
        blocks = (await session.execute(select(m.RiskBlock))).scalars().all()
    assert signals == []
    assert orders == []
    assert any(b.reason == "regime_risk_off" for b in blocks)


@pytest.mark.asyncio
async def test_regime_risk_off_low_confidence_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRETRADE_LLM_CHECK", "false")
    monkeypatch.setenv("RESPECT_REGIME", "true")
    await init_db()
    sf = get_session_factory()
    sig = _signal(ts_ist=(9, 31))
    # confidence just below the 0.7 threshold
    await _seed_regime(
        sf, regime="risk_off", confidence=0.5, ts=sig.breakout_close_time - timedelta(minutes=2)
    )

    broker = PaperBroker(sf)
    engine = ExecutionEngine(broker, sf)
    result = await engine.on_signal(sig)
    assert result is not None  # signal accepted


@pytest.mark.asyncio
async def test_respect_regime_false_disables_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRETRADE_LLM_CHECK", "false")
    monkeypatch.setenv("RESPECT_REGIME", "false")
    await init_db()
    sf = get_session_factory()
    sig = _signal(ts_ist=(9, 31))
    await _seed_regime(
        sf, regime="risk_off", confidence=0.95, ts=sig.breakout_close_time - timedelta(minutes=2)
    )

    broker = PaperBroker(sf)
    engine = ExecutionEngine(broker, sf)
    result = await engine.on_signal(sig)
    assert result is not None


@pytest.mark.asyncio
async def test_pretrade_skip_blocks_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRETRADE_LLM_CHECK", "true")
    monkeypatch.setenv("RESPECT_REGIME", "false")
    await init_db()
    sf = get_session_factory()

    client = StubGeminiClient(
        sf,
        PretradeDecision(decision="skip", size_multiplier=0.0, reason="news suggests downgrade"),
    )
    broker = PaperBroker(sf)
    engine = ExecutionEngine(broker, sf, client)

    result = await engine.on_signal(_signal(ts_ist=(9, 31)))
    assert result is None

    async with sf() as session:
        orders = (await session.execute(select(Order))).scalars().all()
        signals = (await session.execute(select(SignalRow))).scalars().all()
        blocks = (await session.execute(select(m.RiskBlock))).scalars().all()
    assert orders == []
    assert signals == []
    assert any(b.reason == "pretrade_skip" for b in blocks)


@pytest.mark.asyncio
async def test_pretrade_reduce_size_halves_qty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRETRADE_LLM_CHECK", "true")
    monkeypatch.setenv("RESPECT_REGIME", "false")
    await init_db()
    sf = get_session_factory()

    client = StubGeminiClient(
        sf,
        PretradeDecision(decision="reduce_size", size_multiplier=0.5, reason="mixed signals"),
    )
    broker = PaperBroker(sf)
    engine = ExecutionEngine(broker, sf, client)

    # Use a price range where sizing yields qty=25 → reduced to 12.
    sig = _signal(entry=600.0, stop=580.0, ts_ist=(9, 31))
    signal_id = await engine.on_signal(sig)
    assert signal_id is not None

    async with sf() as session:
        sig_row = await session.get(SignalRow, signal_id)
        orders = (await session.execute(select(Order))).scalars().all()
    assert sig_row is not None
    assert sig_row.qty == 12  # int(25 * 0.5)
    assert sig_row.pretrade_decision == "mixed signals"
    assert len(orders) == 1
    assert orders[0].qty == 12


@pytest.mark.asyncio
async def test_engine_works_without_gemini_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """No GEMINI_API_KEY → engine should behave exactly as Phase 3 did."""
    monkeypatch.setenv("PRETRADE_LLM_CHECK", "true")  # toggled on, but no client
    monkeypatch.setenv("RESPECT_REGIME", "true")
    await init_db()
    sf = get_session_factory()
    broker = PaperBroker(sf, slippage_bps=5.0)
    engine = ExecutionEngine(broker, sf, gemini_client=None)
    sig = _signal(entry=600.0, stop=580.0, ts_ist=(9, 31))
    result = await engine.on_signal(sig)
    assert result is not None  # placed despite pretrade_enabled flag

    # Tick to fill
    await engine.on_tick(
        Tick(
            symbol="HDFCBANK-EQ",
            token="1333",
            ltp=600.0,
            ltq=10,
            total_volume=10_000,
            ts=_ist(10, 0),
        )
    )
    async with sf() as session:
        orders = (await session.execute(select(Order))).scalars().all()
    assert len(orders) == 1
    assert orders[0].status == "FILLED"
