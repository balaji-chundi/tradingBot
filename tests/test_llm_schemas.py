from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.llm.schemas import PretradeDecision, RegimeVerdict


def test_regime_verdict_accepts_valid_payload() -> None:
    v = RegimeVerdict.model_validate(
        {
            "regime": "neutral",
            "confidence": 0.55,
            "key_drivers": ["flat breadth", "VIX unchanged"],
            "watch_symbols": ["RELIANCE-EQ"],
            "avoid_symbols": [],
            "rationale": "Mixed signals — no conviction either way.",
        }
    )
    assert v.regime == "neutral"
    assert v.confidence == 0.55


def test_regime_verdict_rejects_unknown_label() -> None:
    with pytest.raises(ValidationError):
        RegimeVerdict.model_validate(
            {
                "regime": "bullish",  # not in Literal
                "confidence": 0.8,
                "key_drivers": ["x"],
                "rationale": "test test test test",
            }
        )


def test_regime_verdict_clamps_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        RegimeVerdict.model_validate(
            {
                "regime": "risk_on",
                "confidence": 1.5,  # > 1
                "key_drivers": ["x"],
                "rationale": "test test test test",
            }
        )


def test_pretrade_decision_accepts_valid_payload() -> None:
    d = PretradeDecision.model_validate(
        {"decision": "reduce_size", "size_multiplier": 0.5, "reason": "regime neutral"}
    )
    assert d.decision == "reduce_size"
    assert d.size_multiplier == 0.5


def test_pretrade_decision_rejects_invalid_multiplier() -> None:
    with pytest.raises(ValidationError):
        PretradeDecision.model_validate(
            {"decision": "proceed", "size_multiplier": 1.5, "reason": "test"}
        )
