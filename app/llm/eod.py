"""Tier 3: end-of-day journal.

Pulls the day's journal rows, hands them to Gemini Pro, validates the
structured response into [[eod-report]], and renders markdown to
`reports/YYYY-MM-DD.md`. Idempotent — re-running overwrites today's file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import IST, PROJECT_ROOT, get_settings
from app.journal import models as m
from app.llm.client import GeminiClient, LLMError, safe_json_dumps
from app.llm.prompts import EOD_PROMPT_VERSION, EOD_SYSTEM, build_eod_user_prompt
from app.llm.schemas import EODReport

log = structlog.get_logger()

REPORTS_DIR = PROJECT_ROOT / "reports"


@dataclass(slots=True)
class _DayWindow:
    start_utc: datetime
    end_utc: datetime
    ist_date: date


def _window_for_ist_date(ist_date: date) -> _DayWindow:
    start_ist = datetime.combine(ist_date, time(0, 0), tzinfo=IST)
    end_ist = start_ist + timedelta(days=1)
    return _DayWindow(
        start_utc=start_ist.astimezone(UTC),
        end_utc=end_ist.astimezone(UTC),
        ist_date=ist_date,
    )


async def build_eod_context(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ist_date: date,
) -> dict[str, Any]:
    """Pull every relevant row for `ist_date` and shape it for the prompt."""
    window = _window_for_ist_date(ist_date)
    async with session_factory() as session:
        signals = await _signals(session, window)
        positions = await _positions(session, window)
        fills = await _fills(session, window)
        regimes = await _regimes(session, window)
        blocks = await _blocks(session, window)

    realised = sum(p["realised_pnl"] for p in positions if p["closed_at_ist"])
    charges = sum(f["charges"] for f in fills)
    wins = sum(1 for p in positions if (p["realised_pnl"] or 0.0) > 0)
    losses = sum(1 for p in positions if (p["realised_pnl"] or 0.0) < 0)

    return {
        "date_ist": ist_date.isoformat(),
        "summary": {
            "signals_total": len(signals),
            "signals_blocked": sum(1 for s in signals if s["status"] == "BLOCKED"),
            "signals_submitted": sum(1 for s in signals if s["status"] != "BLOCKED"),
            "positions_opened": len(positions),
            "positions_closed": sum(1 for p in positions if p["closed_at_ist"]),
            "wins": wins,
            "losses": losses,
            "realised_pnl_inr": round(realised, 2),
            "charges_inr": round(charges, 2),
            "net_pnl_inr": round(realised, 2),  # realised already nets charges per close
            "regime_calls_total": len(regimes),
            "risk_blocks_total": len(blocks),
        },
        "signals": signals,
        "positions": positions,
        "fills": fills,
        "regime_verdicts": regimes,
        "risk_blocks": blocks,
    }


async def run_eod_report(
    *,
    client: GeminiClient,
    session_factory: async_sessionmaker[AsyncSession],
    ist_date: date,
    out_path: Path | None = None,
) -> tuple[EODReport, Path]:
    """Build context, call Gemini, write markdown. Returns (parsed, file path)."""
    settings = get_settings()
    context = await build_eod_context(session_factory, ist_date=ist_date)
    try:
        report, _ = await client.generate_json(
            model=settings.gemini_model_tier1,
            system_instruction=EOD_SYSTEM,
            user_prompt=build_eod_user_prompt(safe_json_dumps(context)),
            response_schema=EODReport,
            prompt_version=EOD_PROMPT_VERSION,
            tier="eod",
            temperature=0.3,
        )
    except LLMError as e:
        log.error("eod_report_failed", error=str(e), ist_date=str(ist_date))
        raise

    path = out_path or (REPORTS_DIR / f"{ist_date.isoformat()}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_markdown(report, context))
    log.info("eod_report_written", path=str(path), ist_date=str(ist_date))
    return report, path


def format_markdown(report: EODReport, context: dict[str, Any]) -> str:
    s = context["summary"]
    lines: list[str] = []
    lines.append(f"# EOD Report — {context['date_ist']}")
    lines.append("")
    lines.append(f"**{report.headline}**")
    lines.append("")
    lines.append("## Summary")
    lines.append(report.summary)
    lines.append("")
    lines.append("## Day at a glance")
    lines.append("")
    lines.append(
        f"- Signals: {s['signals_total']} "
        f"({s['signals_submitted']} submitted, {s['signals_blocked']} blocked)"
    )
    lines.append(f"- Positions opened/closed: {s['positions_opened']} / {s['positions_closed']}")
    lines.append(f"- Wins / Losses: {s['wins']} / {s['losses']}")
    lines.append(f"- Realised P&L (net of charges): ₹{s['net_pnl_inr']}")
    lines.append(f"- Charges paid: ₹{s['charges_inr']}")
    lines.append(f"- Regime calls: {s['regime_calls_total']}")
    lines.append(f"- Risk blocks: {s['risk_blocks_total']}")
    lines.append("")

    if context["positions"]:
        lines.append("## Trades")
        lines.append("")
        lines.append("| Time (IST) | Symbol | Side | Qty | Entry | Exit | Net P&L |")
        lines.append("|---|---|---|---|---:|---:|---:|")
        for p in context["positions"]:
            opened = p["opened_at_ist"] or "—"
            closed = p["closed_at_ist"] or "open"
            exit_px = p.get("exit_price")
            exit_str = f"{exit_px:.2f}" if exit_px else "—"
            lines.append(
                f"| {opened} → {closed} | {p['symbol']} | {p['direction']} "
                f"| {p['qty']} | {p['entry_price']:.2f} "
                f"| {exit_str} | ₹{p['realised_pnl']:.2f} |"
            )
        lines.append("")

    if report.what_worked:
        lines.append("## What worked")
        for x in report.what_worked:
            lines.append(f"- {x}")
        lines.append("")

    if report.what_didnt_work:
        lines.append("## What didn't")
        for x in report.what_didnt_work:
            lines.append(f"- {x}")
        lines.append("")

    if context["regime_verdicts"]:
        lines.append("## Regime calls")
        lines.append("")
        lines.append("| Time (IST) | Regime | Confidence | Drivers |")
        lines.append("|---|---|---:|---|")
        for r in context["regime_verdicts"]:
            drivers = ", ".join(r.get("key_drivers", [])[:3])
            lines.append(f"| {r['ts_ist']} | {r['regime']} | {r['confidence']:.2f} | {drivers} |")
        lines.append("")

    lines.append("## Regime accuracy")
    lines.append(report.regime_accuracy)
    lines.append("")

    if report.parameter_suggestions:
        lines.append("## Suggestions — parameters")
        for x in report.parameter_suggestions:
            lines.append(f"- {x}")
        lines.append("")

    if report.universe_suggestions:
        lines.append("## Suggestions — universe")
        for x in report.universe_suggestions:
            lines.append(f"- {x}")
        lines.append("")

    return "\n".join(lines)


# ----- DB helpers ---------------------------------------------------------------


async def _signals(session: AsyncSession, w: _DayWindow) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(m.Signal)
                .where(m.Signal.created_at >= w.start_utc)
                .where(m.Signal.created_at < w.end_utc)
                .order_by(m.Signal.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "symbol": r.symbol,
            "direction": r.direction,
            "entry": round(r.breakout_price, 2),
            "stop": round(r.stop, 2),
            "target": round(r.target, 2),
            "qty": r.qty,
            "status": r.status,
            "pretrade_decision": r.pretrade_decision,
            "ts_ist": r.created_at.astimezone(IST).strftime("%H:%M:%S"),
        }
        for r in rows
    ]


async def _positions(session: AsyncSession, w: _DayWindow) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(m.Position)
                .where(m.Position.opened_at >= w.start_utc)
                .where(m.Position.opened_at < w.end_utc)
                .order_by(m.Position.opened_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "symbol": r.symbol,
            "direction": "long" if r.qty > 0 else "short",
            "qty": abs(r.qty),
            "entry_price": round(r.avg_entry, 2),
            "opened_at_ist": r.opened_at.astimezone(IST).strftime("%H:%M:%S"),
            "closed_at_ist": (
                r.closed_at.astimezone(IST).strftime("%H:%M:%S") if r.closed_at else None
            ),
            "realised_pnl": round(float(r.realised_pnl or 0.0), 2),
        }
        for r in rows
    ]


async def _fills(session: AsyncSession, w: _DayWindow) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(m.Fill, m.Order, m.SlippageLog)
            .join(m.Order, m.Order.id == m.Fill.order_id)
            .outerjoin(m.SlippageLog, m.SlippageLog.order_id == m.Fill.order_id)
            .where(m.Fill.ts >= w.start_utc)
            .where(m.Fill.ts < w.end_utc)
            .order_by(m.Fill.ts.asc())
        )
    ).all()
    out: list[dict[str, Any]] = []
    for fill, order, slip in rows:
        out.append(
            {
                "symbol": order.symbol,
                "side": order.side,
                "qty": fill.qty,
                "fill_price": round(fill.price, 2),
                "ideal_price": round(slip.ideal_price, 2) if slip else None,
                "slippage_bps": round(slip.slippage_bps, 1) if slip else None,
                "charges": round(fill.charges_inr, 2),
                "role": (order.payload or {}).get("role") if order.payload else None,
                "ts_ist": fill.ts.astimezone(IST).strftime("%H:%M:%S"),
            }
        )
    return out


async def _regimes(session: AsyncSession, w: _DayWindow) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(m.RegimeVerdict)
                .where(m.RegimeVerdict.ts >= w.start_utc)
                .where(m.RegimeVerdict.ts < w.end_utc)
                .order_by(m.RegimeVerdict.ts.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "ts_ist": r.ts.astimezone(IST).strftime("%H:%M:%S"),
            "regime": r.regime,
            "confidence": round(r.confidence, 2),
            "key_drivers": list(r.key_drivers or []),
            "rationale": r.rationale,
        }
        for r in rows
    ]


async def _blocks(session: AsyncSession, w: _DayWindow) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(m.RiskBlock)
                .where(m.RiskBlock.ts >= w.start_utc)
                .where(m.RiskBlock.ts < w.end_utc)
                .order_by(m.RiskBlock.ts.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "ts_ist": r.ts.astimezone(IST).strftime("%H:%M:%S"),
            "reason": r.reason,
            "symbol": (r.payload or {}).get("symbol", "—"),
        }
        for r in rows
    ]
