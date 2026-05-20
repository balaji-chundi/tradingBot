"""Pretrade Tier-2 fail-open behaviour.

The brief mandates: if the LLM times out or errors, the engine must NOT block
trading. We exercise both paths with a stub client that overrides
`generate_json`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from app.config import IST
from app.data.types import Signal
from app.journal.db import get_session_factory, init_db
from app.llm.client import GeminiClient, LLMError, LLMTimeout
from app.llm.pretrade import DEFAULT_PROCEED, run_pretrade_check
from app.llm.schemas import PretradeDecision


class StubGeminiClient(GeminiClient):
    def __init__(
        self, sf: Any, *, response: BaseModel | None = None, error: Exception | None = None
    ) -> None:
        super().__init__(sf)
        self._stub_response = response
        self._stub_error = error
        self._last_call_id = 999

    async def generate_json(self, **kwargs: Any) -> tuple[Any, int]:  # type: ignore[override]
        if self._stub_error is not None:
            raise self._stub_error
        assert self._stub_response is not None
        return self._stub_response, 999


def _sig() -> Signal:
    ts = datetime(2026, 5, 20, 9, 31, tzinfo=IST).astimezone(UTC)
    return Signal(
        symbol="HDFCBANK-EQ",
        direction="long",
        breakout_close_time=ts,
        breakout_price=700.0,
        or_high=700.0,
        or_low=680.0,
        stop=680.0,
        target=730.0,
        bar_volume=10_000,
        avg_prior_5bar_volume=5_000,
        volume_ratio=2.0,
    )


@pytest.mark.asyncio
async def test_pretrade_returns_proceed_on_timeout() -> None:
    await init_db()
    sf = get_session_factory()
    client = StubGeminiClient(sf, error=LLMTimeout("simulated timeout"))
    out = await run_pretrade_check(client=client, session_factory=sf, signal=_sig(), qty=10)
    assert out.decision == "proceed"
    assert out.size_multiplier == 1.0
    assert out == DEFAULT_PROCEED


@pytest.mark.asyncio
async def test_pretrade_returns_proceed_on_error() -> None:
    await init_db()
    sf = get_session_factory()
    client = StubGeminiClient(sf, error=LLMError("validation failed"))
    out = await run_pretrade_check(client=client, session_factory=sf, signal=_sig(), qty=10)
    assert out == DEFAULT_PROCEED


@pytest.mark.asyncio
async def test_pretrade_passes_through_real_decision() -> None:
    await init_db()
    sf = get_session_factory()
    client = StubGeminiClient(
        sf,
        response=PretradeDecision(
            decision="reduce_size",
            size_multiplier=0.5,
            reason="regime neutral; mediocre volume",
        ),
    )
    out = await run_pretrade_check(client=client, session_factory=sf, signal=_sig(), qty=10)
    assert out.decision == "reduce_size"
    assert out.size_multiplier == 0.5
