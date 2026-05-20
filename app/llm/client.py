"""Gemini API wrapper.

Every call is persisted to `llm_calls` (full prompt + response + latency +
token counts) so we can audit/replay later. The wrapper exposes a single
`generate_json` async method that returns a validated pydantic instance and
logs to the journal regardless of success.

A `timeout_s` argument is honored via asyncio.wait_for — Tier 2 sets this to
2.5s and falls back to "proceed" on timeout.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.journal import models as m

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Raised when the API call failed AND the caller should not silently fall back."""


class LLMTimeout(LLMError):
    """The call took longer than `timeout_s`."""


class GeminiClient:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        settings = get_settings()
        self._api_key = settings.gemini_api_key
        # Lazy SDK import keeps test collection fast and gives a cleaner error
        # if the package is missing at runtime.
        self._client_cache: Any | None = None

    def _get_client(self) -> Any:
        if self._client_cache is None:
            if not self._api_key:
                raise LLMError("GEMINI_API_KEY is not set in .env")
            from google import genai

            self._client_cache = genai.Client(api_key=self._api_key)
        return self._client_cache

    async def generate_json(
        self,
        *,
        model: str,
        system_instruction: str,
        user_prompt: str,
        response_schema: type[T],
        prompt_version: str,
        tier: str,
        timeout_s: float | None = None,
        temperature: float = 0.2,
    ) -> tuple[T, int]:
        """Call Gemini, parse the JSON into `response_schema`, persist the call.

        Returns (parsed_object, llm_call_id). The llm_call_id lets callers link
        their domain row (e.g. regime_verdicts.llm_call_id) back to the audit
        trail.

        Raises LLMTimeout on timeout, LLMError on any other failure.
        """
        full_prompt = f"{system_instruction}\n\n{user_prompt}"
        started = time.monotonic()
        response_text: str | None = None
        error: str | None = None
        tokens_in: int | None = None
        tokens_out: int | None = None
        parsed: T | None = None
        try:
            response_text = await self._call_with_timeout(
                model=model,
                system_instruction=system_instruction,
                user_prompt=user_prompt,
                response_schema=response_schema,
                temperature=temperature,
                timeout_s=timeout_s,
            )
            usage = self._extract_usage_last_call()
            tokens_in, tokens_out = usage
            parsed = response_schema.model_validate_json(response_text)
        except TimeoutError as e:
            error = f"timeout: {e}"
            raise LLMTimeout(error) from e
        except ValidationError as e:
            error = f"validation: {e}"
            raise LLMError(error) from e
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise LLMError(error) from e
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            llm_call_id = await self._persist_call(
                tier=tier,
                model=model,
                prompt_version=prompt_version,
                prompt=full_prompt,
                response=response_text,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                error=error,
            )
            log.info(
                "llm_call",
                tier=tier,
                model=model,
                version=prompt_version,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                error=error,
                llm_call_id=llm_call_id,
            )
            self._last_call_id = llm_call_id
        assert parsed is not None  # unreachable if no exception was raised
        return parsed, self._last_call_id

    async def _call_with_timeout(
        self,
        *,
        model: str,
        system_instruction: str,
        user_prompt: str,
        response_schema: type[BaseModel],
        temperature: float,
        timeout_s: float | None,
    ) -> str:
        # google-genai's async client lives at client.aio.models
        from google.genai import types

        client = self._get_client()
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=response_schema,
            system_instruction=system_instruction,
            temperature=temperature,
        )
        coro = client.aio.models.generate_content(
            model=model,
            contents=user_prompt,
            config=config,
        )
        if timeout_s is None:
            resp = await coro
        else:
            resp = await asyncio.wait_for(coro, timeout=timeout_s)
        self._last_response = resp
        text = getattr(resp, "text", None)
        if not text:
            # Some SDK shapes nest the JSON in candidates[0].content.parts[0].text
            cands = getattr(resp, "candidates", None) or []
            if cands and hasattr(cands[0], "content"):
                parts = getattr(cands[0].content, "parts", []) or []
                if parts and hasattr(parts[0], "text"):
                    text = parts[0].text
        if not text:
            raise LLMError("empty response from Gemini")
        return str(text)

    def _extract_usage_last_call(self) -> tuple[int | None, int | None]:
        resp = getattr(self, "_last_response", None)
        if resp is None:
            return None, None
        usage = getattr(resp, "usage_metadata", None)
        if usage is None:
            return None, None
        return (
            getattr(usage, "prompt_token_count", None),
            getattr(usage, "candidates_token_count", None),
        )

    async def _persist_call(
        self,
        *,
        tier: str,
        model: str,
        prompt_version: str,
        prompt: str,
        response: str | None,
        latency_ms: int,
        tokens_in: int | None,
        tokens_out: int | None,
        error: str | None,
    ) -> int:
        async with self._sf() as session:
            row = m.LLMCall(
                ts=datetime.now(UTC),
                tier=tier,
                model=model,
                prompt_version=prompt_version,
                prompt=prompt,
                response=response,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                error=error,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return int(row.id)


def safe_json_dumps(payload: Any) -> str:
    return json.dumps(payload, default=str, indent=2)
