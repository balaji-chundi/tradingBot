"""EOD report context builder + markdown formatter (LLM call stubbed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.config import IST
from app.journal import models as m
from app.journal.db import get_session_factory, init_db
from app.llm.client import GeminiClient
from app.llm.eod import build_eod_context, format_markdown, run_eod_report
from app.llm.schemas import EODReport


class StubGemini(GeminiClient):
    def __init__(self, sf: Any, response: BaseModel) -> None:
        super().__init__(sf)
        self._resp = response

    async def generate_json(self, **kwargs: Any) -> tuple[Any, int]:  # type: ignore[override]
        return self._resp, 7


@pytest.mark.asyncio
async def test_build_context_filters_by_ist_day() -> None:
    await init_db()
    sf = get_session_factory()
    today_ist = datetime(2026, 5, 21, 10, 0, tzinfo=IST)
    yesterday_ist = today_ist - timedelta(days=1)

    async with sf() as session:
        # Today's signal
        session.add(
            m.Signal(
                symbol="HDFCBANK-EQ",
                direction="long",
                breakout_price=700.0,
                or_high=700.0,
                or_low=680.0,
                qty=10,
                stop=680.0,
                target=730.0,
                status="FILLED",
                created_at=today_ist.astimezone(UTC),
            )
        )
        # Yesterday's signal — should be excluded
        session.add(
            m.Signal(
                symbol="TCS-EQ",
                direction="short",
                breakout_price=2300.0,
                or_high=2310.0,
                or_low=2300.0,
                qty=5,
                stop=2310.0,
                target=2285.0,
                status="FILLED",
                created_at=yesterday_ist.astimezone(UTC),
            )
        )
        # Today's regime verdict
        session.add(
            m.RegimeVerdict(
                ts=today_ist.astimezone(UTC),
                regime="neutral",
                confidence=0.6,
                key_drivers=["flat breadth"],
                watch_symbols=[],
                avoid_symbols=[],
                rationale="mixed signals",
            )
        )
        # Today's risk block
        session.add(
            m.RiskBlock(
                ts=today_ist.astimezone(UTC),
                reason="regime_risk_off",
                payload={"symbol": "INFY-EQ"},
            )
        )
        await session.commit()

    ctx = await build_eod_context(sf, ist_date=today_ist.date())
    assert ctx["summary"]["signals_total"] == 1
    assert ctx["signals"][0]["symbol"] == "HDFCBANK-EQ"
    assert ctx["summary"]["regime_calls_total"] == 1
    assert ctx["summary"]["risk_blocks_total"] == 1


@pytest.mark.asyncio
async def test_format_markdown_renders_key_sections() -> None:
    report = EODReport(
        headline="Day was quiet — 0 trades, 2 risk_off regime calls held us back.",
        summary=(
            "The strategy emitted no signals because OR breakouts lacked the "
            "1.5x volume confirmation."
        ),
        what_worked=["Risk gate correctly held us out during high-VIX periods."],
        what_didnt_work=["No fills — but that's by design today."],
        regime_accuracy="risk_off calls aligned with sideways Nifty session.",
        parameter_suggestions=["Consider 1.3x volume threshold."],
        universe_suggestions=[],
    )
    context = {
        "date_ist": "2026-05-21",
        "summary": {
            "signals_total": 0,
            "signals_blocked": 0,
            "signals_submitted": 0,
            "positions_opened": 0,
            "positions_closed": 0,
            "wins": 0,
            "losses": 0,
            "realised_pnl_inr": 0.0,
            "charges_inr": 0.0,
            "net_pnl_inr": 0.0,
            "regime_calls_total": 22,
            "risk_blocks_total": 3,
        },
        "positions": [],
        "regime_verdicts": [
            {
                "ts_ist": "09:45:00",
                "regime": "neutral",
                "confidence": 0.55,
                "key_drivers": ["flat breadth", "VIX unchanged"],
                "rationale": "mixed",
            }
        ],
    }
    md = format_markdown(report, context)
    assert "# EOD Report — 2026-05-21" in md
    assert "Day was quiet" in md
    assert "## Summary" in md
    assert "## Regime calls" in md
    assert "neutral" in md
    assert "Consider 1.3x volume threshold" in md


@pytest.mark.asyncio
async def test_run_eod_report_writes_file_with_stubbed_llm(tmp_path: Path) -> None:
    await init_db()
    sf = get_session_factory()
    # Seed a single closed position so realised P&L > 0.
    async with sf() as session:
        opened = datetime(2026, 5, 21, 10, 0, tzinfo=IST).astimezone(UTC)
        closed = opened + timedelta(minutes=30)
        session.add(
            m.Position(
                symbol="HDFCBANK-EQ",
                qty=10,
                avg_entry=700.0,
                opened_at=opened,
                closed_at=closed,
                realised_pnl=234.56,
            )
        )
        await session.commit()

    report = EODReport(
        headline="One winner today, 1.0R captured.",
        summary="HDFCBANK long target hit at 730.",
        what_worked=["Clean breakout with volume confirmation."],
        what_didnt_work=[],
        regime_accuracy="risk_on call held through entry; correct.",
        parameter_suggestions=[],
        universe_suggestions=[],
    )
    out = tmp_path / "report.md"
    parsed, path = await run_eod_report(
        client=StubGemini(sf, report),
        session_factory=sf,
        ist_date=opened.astimezone(IST).date(),
        out_path=out,
    )
    assert path == out
    assert path.exists()
    body = path.read_text()
    assert "One winner today" in body
    assert "HDFCBANK-EQ" in body
    assert "234.56" in body
    assert parsed.headline == report.headline
