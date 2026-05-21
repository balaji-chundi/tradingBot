"""Pydantic contracts for the strict-JSON LLM outputs.

These doubles as Gemini structured-output `response_schema` and as the
in-process types the rest of the app handles.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RegimeLabel = Literal["risk_on", "neutral", "risk_off"]
PretradeChoice = Literal["proceed", "skip", "reduce_size"]


class RegimeVerdict(BaseModel):
    """Tier 1 output: the 15-min market regime call."""

    regime: RegimeLabel
    confidence: float = Field(ge=0.0, le=1.0)
    key_drivers: list[str] = Field(min_length=1, max_length=8)
    watch_symbols: list[str] = Field(default_factory=list, max_length=10)
    avoid_symbols: list[str] = Field(default_factory=list, max_length=10)
    rationale: str = Field(min_length=10, max_length=2000)


class PretradeDecision(BaseModel):
    """Tier 2 output: per-signal gut check."""

    decision: PretradeChoice
    size_multiplier: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=4, max_length=400)


class EODReport(BaseModel):
    """Tier 3 output: end-of-day journal. Persisted as markdown to reports/."""

    headline: str = Field(min_length=10, max_length=240)
    summary: str = Field(min_length=20, max_length=2000)
    what_worked: list[str] = Field(default_factory=list, max_length=10)
    what_didnt_work: list[str] = Field(default_factory=list, max_length=10)
    regime_accuracy: str = Field(min_length=10, max_length=2000)
    parameter_suggestions: list[str] = Field(default_factory=list, max_length=10)
    universe_suggestions: list[str] = Field(default_factory=list, max_length=10)
