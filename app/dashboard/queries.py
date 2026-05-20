"""Read-only DB helpers backing the dashboard partials.

Everything the dashboard surfaces is already persisted by the orchestrator
/ engine / broker / LLM, so the dashboard doesn't touch the live in-memory
state — that keeps it independent of FastAPI request lifecycle and easier
to test against a seeded DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import IST
from app.journal import models as m
from app.strategy.universe import NIFTY_5_UNIVERSE

# Local alias to keep signatures readable; resolved lazily under
# `from __future__ import annotations`.
_SF = async_sessionmaker[AsyncSession]


def today_start_utc(now_utc: datetime | None = None) -> datetime:
    now = (now_utc or datetime.now(UTC)).astimezone(IST)
    return datetime.combine(now.date(), time(0, 0), tzinfo=IST).astimezone(UTC)


def week_start_utc(now_utc: datetime | None = None) -> datetime:
    now = (now_utc or datetime.now(UTC)).astimezone(IST)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday.astimezone(UTC)


# ---------- summaries ----------


@dataclass(frozen=True, slots=True)
class PnLSummary:
    capital_inr: float
    realised_today: float
    realised_week: float
    trades_today: int
    open_position_count: int


async def get_pnl_summary(sf: _SF, *, capital_inr: float) -> PnLSummary:
    today_utc = today_start_utc()
    week_utc = week_start_utc()
    async with sf() as session:
        realised_today = (
            await session.execute(
                select(func.coalesce(func.sum(m.Position.realised_pnl), 0.0))
                .where(m.Position.closed_at.is_not(None))
                .where(m.Position.closed_at >= today_utc)
            )
        ).scalar_one()
        realised_week = (
            await session.execute(
                select(func.coalesce(func.sum(m.Position.realised_pnl), 0.0))
                .where(m.Position.closed_at.is_not(None))
                .where(m.Position.closed_at >= week_utc)
            )
        ).scalar_one()
        trades_today = (
            await session.execute(
                select(func.count(m.Signal.id))
                .where(m.Signal.created_at >= today_utc)
                .where(m.Signal.status != "BLOCKED")
            )
        ).scalar_one()
        open_count = (
            await session.execute(
                select(func.count(m.Position.id)).where(m.Position.closed_at.is_(None))
            )
        ).scalar_one()
    return PnLSummary(
        capital_inr=capital_inr,
        realised_today=float(realised_today),
        realised_week=float(realised_week),
        trades_today=int(trades_today),
        open_position_count=int(open_count),
    )


# ---------- tables ----------


async def get_open_positions(sf: _SF) -> list[dict[str, Any]]:
    async with sf() as session:
        rows = (
            (await session.execute(select(m.Position).where(m.Position.closed_at.is_(None))))
            .scalars()
            .all()
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        direction = "long" if r.qty > 0 else "short"
        out.append(
            {
                "id": r.id,
                "symbol": r.symbol,
                "direction": direction,
                "qty": abs(r.qty),
                "avg_entry": round(r.avg_entry, 2),
                "opened_at_ist": r.opened_at.astimezone(IST).strftime("%H:%M:%S"),
            }
        )
    return out


async def get_today_signals(sf: _SF, *, limit: int = 25) -> list[dict[str, Any]]:
    today_utc = today_start_utc()
    async with sf() as session:
        rows = (
            (
                await session.execute(
                    select(m.Signal)
                    .where(m.Signal.created_at >= today_utc)
                    .order_by(desc(m.Signal.created_at))
                    .limit(limit)
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
            "qty": r.qty,
            "entry": round(r.breakout_price, 2),
            "stop": round(r.stop, 2),
            "target": round(r.target, 2),
            "status": r.status,
            "pretrade_decision": r.pretrade_decision or "",
            "ts_ist": r.created_at.astimezone(IST).strftime("%H:%M:%S"),
        }
        for r in rows
    ]


async def get_latest_regime(sf: _SF) -> dict[str, Any] | None:
    async with sf() as session:
        row = (
            await session.execute(
                select(m.RegimeVerdict).order_by(desc(m.RegimeVerdict.ts)).limit(1)
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "regime": row.regime,
        "confidence": round(row.confidence, 2),
        "ts_ist": row.ts.astimezone(IST).strftime("%H:%M:%S"),
        "key_drivers": list(row.key_drivers or []),
        "watch_symbols": list(row.watch_symbols or []),
        "avoid_symbols": list(row.avoid_symbols or []),
        "rationale": row.rationale,
    }


async def get_recent_fills(sf: _SF, *, limit: int = 15) -> list[dict[str, Any]]:
    async with sf() as session:
        rows = (
            await session.execute(
                select(m.Fill, m.Order, m.SlippageLog)
                .join(m.Order, m.Order.id == m.Fill.order_id)
                .outerjoin(m.SlippageLog, m.SlippageLog.order_id == m.Fill.order_id)
                .order_by(desc(m.Fill.ts))
                .limit(limit)
            )
        ).all()
    out: list[dict[str, Any]] = []
    for fill, order, slip in rows:
        role = (order.payload or {}).get("role") if order.payload else None
        out.append(
            {
                "fill_id": fill.id,
                "order_id": order.id,
                "symbol": order.symbol,
                "side": order.side,
                "qty": fill.qty,
                "fill_price": round(fill.price, 2),
                "ideal_price": round(slip.ideal_price, 2) if slip else None,
                "slippage_bps": round(slip.slippage_bps, 1) if slip else None,
                "charges": round(fill.charges_inr, 2),
                "role": role,
                "ts_ist": fill.ts.astimezone(IST).strftime("%H:%M:%S"),
            }
        )
    return out


@dataclass(frozen=True, slots=True)
class SlippageStats:
    count: int
    avg_bps: float | None
    max_abs_bps: float | None


async def get_slippage_stats(sf: _SF) -> SlippageStats:
    today_utc = today_start_utc()
    async with sf() as session:
        result = (
            await session.execute(
                select(
                    func.count(m.SlippageLog.id),
                    func.avg(m.SlippageLog.slippage_bps),
                    func.max(func.abs(m.SlippageLog.slippage_bps)),
                ).where(m.SlippageLog.ts >= today_utc)
            )
        ).one()
    count, avg, mx = result
    return SlippageStats(
        count=int(count or 0),
        avg_bps=float(avg) if avg is not None else None,
        max_abs_bps=float(mx) if mx is not None else None,
    )


async def get_risk_blocks_today(sf: _SF, *, limit: int = 25) -> list[dict[str, Any]]:
    today_utc = today_start_utc()
    async with sf() as session:
        rows = (
            (
                await session.execute(
                    select(m.RiskBlock)
                    .where(m.RiskBlock.ts >= today_utc)
                    .order_by(desc(m.RiskBlock.ts))
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": r.id,
            "reason": r.reason,
            "symbol": (r.payload or {}).get("symbol", "—"),
            "ts_ist": r.ts.astimezone(IST).strftime("%H:%M:%S"),
        }
        for r in rows
    ]


async def get_last_ticks_per_symbol(sf: _SF) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async with sf() as session:
        for symbol in NIFTY_5_UNIVERSE:
            row = (
                await session.execute(
                    select(m.Tick).where(m.Tick.symbol == symbol).order_by(desc(m.Tick.ts)).limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                out.append({"symbol": symbol, "ltp": None, "ts_ist": "—"})
            else:
                out.append(
                    {
                        "symbol": symbol,
                        "ltp": round(row.ltp, 2),
                        "ts_ist": row.ts.astimezone(IST).strftime("%H:%M:%S"),
                    }
                )
    return out
