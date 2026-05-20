"""Tier 1: 15-min market-regime check.

Scheduled by app.scheduler at 09:30, 09:45, ..., 15:00 IST. Each run assembles
context from the journal (5-stock universe snapshot + open positions + headlines),
calls Gemini Pro with the strict-JSON schema, and writes a regime_verdicts row
linked to the llm_calls row for audit.

Engine reads the latest verdict at signal time; if `risk_off` with confidence
> 0.7 and `respect_regime=True`, new entries are blocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.journal import models as m
from app.llm.client import GeminiClient, LLMError, safe_json_dumps
from app.llm.context import build_regime_context
from app.llm.news import fetch_headlines, titles
from app.llm.prompts import REGIME_PROMPT_VERSION, REGIME_SYSTEM, build_regime_user_prompt
from app.llm.schemas import RegimeVerdict
from app.strategy.universe import NIFTY_5_UNIVERSE

log = structlog.get_logger()


async def run_regime_check(
    *,
    client: GeminiClient,
    session_factory: async_sessionmaker[AsyncSession],
    universe: list[str] | None = None,
    open_positions_summary: list[dict[str, Any]] | None = None,
    realised_pnl_today: float = 0.0,
    unrealised_pnl_today: float = 0.0,
    now_utc: datetime | None = None,
) -> RegimeVerdict | None:
    settings = get_settings()
    universe = universe or NIFTY_5_UNIVERSE

    headlines = await fetch_headlines()
    context = await build_regime_context(
        session_factory,
        universe=universe,
        open_positions_summary=open_positions_summary,
        realised_pnl_today=realised_pnl_today,
        unrealised_pnl_today=unrealised_pnl_today,
        news_headlines=titles(headlines),
        now_utc=now_utc,
    )

    try:
        verdict, llm_call_id = await client.generate_json(
            model=settings.gemini_model_tier1,
            system_instruction=REGIME_SYSTEM,
            user_prompt=build_regime_user_prompt(safe_json_dumps(context)),
            response_schema=RegimeVerdict,
            prompt_version=REGIME_PROMPT_VERSION,
            tier="regime",
        )
    except LLMError as e:
        log.error("regime_check_failed", error=str(e))
        return None

    await _persist_verdict(session_factory, verdict, llm_call_id)
    log.info(
        "regime_verdict",
        regime=verdict.regime,
        confidence=verdict.confidence,
        drivers=verdict.key_drivers,
    )
    return verdict


async def _persist_verdict(
    session_factory: async_sessionmaker[AsyncSession],
    verdict: RegimeVerdict,
    llm_call_id: int,
) -> None:
    async with session_factory() as session:
        session.add(
            m.RegimeVerdict(
                ts=datetime.now(UTC),
                regime=verdict.regime,
                confidence=verdict.confidence,
                key_drivers=list(verdict.key_drivers),
                watch_symbols=list(verdict.watch_symbols),
                avoid_symbols=list(verdict.avoid_symbols),
                rationale=verdict.rationale,
                llm_call_id=llm_call_id,
            )
        )
        await session.commit()
