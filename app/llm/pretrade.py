"""Tier 2: per-signal pre-trade check.

Called from ExecutionEngine.on_signal *after* sizing succeeds, *before* the
entry order is placed. Hard 2.5-second timeout. Fail-open: any timeout, API
error, or schema-validation failure returns `proceed` with size_multiplier=1.0
so the LLM never gates trading on its own availability — that's the brief's
explicit instruction.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.data.types import Signal as SignalT
from app.llm.client import GeminiClient, LLMError, safe_json_dumps
from app.llm.context import build_pretrade_context, latest_regime_dict
from app.llm.news import fetch_headlines, filter_recent_about_symbol
from app.llm.prompts import (
    PRETRADE_PROMPT_VERSION,
    PRETRADE_SYSTEM,
    build_pretrade_user_prompt,
)
from app.llm.schemas import PretradeDecision

log = structlog.get_logger()

PRETRADE_TIMEOUT_S = 2.5
DEFAULT_PROCEED = PretradeDecision(
    decision="proceed",
    size_multiplier=1.0,
    reason="LLM unavailable or timed out; failing open per Section 7.",
)


async def run_pretrade_check(
    *,
    client: GeminiClient,
    session_factory: async_sessionmaker[AsyncSession],
    signal: SignalT,
    qty: int,
) -> PretradeDecision:
    settings = get_settings()

    # Pull the latest regime verdict (within 30 min) for the prompt context.
    regime = await latest_regime_dict(session_factory)

    # Best-effort RSS pull; if it fails, we still call with an empty list.
    headlines = await fetch_headlines()
    symbol_news = filter_recent_about_symbol(headlines, symbol=signal.symbol, minutes=30)

    context = await build_pretrade_context(
        session_factory,
        signal=signal,
        qty=qty,
        latest_regime=regime,
        news_headlines=symbol_news,
    )

    try:
        decision, _ = await client.generate_json(
            model=settings.gemini_model_tier2,
            system_instruction=PRETRADE_SYSTEM,
            user_prompt=build_pretrade_user_prompt(safe_json_dumps(context)),
            response_schema=PretradeDecision,
            prompt_version=PRETRADE_PROMPT_VERSION,
            tier="pretrade",
            timeout_s=PRETRADE_TIMEOUT_S,
            temperature=0.1,
        )
    except LLMError as e:
        log.warning(
            "pretrade_fail_open",
            symbol=signal.symbol,
            error=str(e),
        )
        return DEFAULT_PROCEED

    log.info(
        "pretrade_decision",
        symbol=signal.symbol,
        decision=decision.decision,
        size_multiplier=decision.size_multiplier,
        reason=decision.reason,
    )
    return decision
