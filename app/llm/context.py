"""Assemble structured inputs for Tier 1 and Tier 2 LLM calls.

Phase 4 v1 limitation: Nifty 50 spot, India VIX, and sector breadth aren't
available (we only subscribe to 5 EQ tokens). We build a 5-stock proxy and
explicitly tell the LLM what's missing in `note`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import IST
from app.data.types import Signal as SignalT
from app.journal import models as m


async def build_regime_context(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    universe: list[str],
    open_positions_summary: list[dict[str, Any]] | None = None,
    realised_pnl_today: float = 0.0,
    unrealised_pnl_today: float = 0.0,
    news_headlines: list[str] | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Build the regime-input dict the prompt template renders."""
    now = now_utc or datetime.now(UTC)
    today_open_ist = datetime(
        now.astimezone(IST).year,
        now.astimezone(IST).month,
        now.astimezone(IST).day,
        9,
        15,
        tzinfo=IST,
    )
    today_open_utc = today_open_ist.astimezone(UTC)

    universe_snapshot: list[dict[str, Any]] = []
    up = 0
    down = 0
    async with session_factory() as session:
        for symbol in universe:
            snap = await _symbol_snapshot(session, symbol, today_open_utc)
            universe_snapshot.append(snap)
            change = snap.get("pct_change_today")
            if change is not None:
                if change > 0:
                    up += 1
                elif change < 0:
                    down += 1

    return {
        "timestamp_ist": now.astimezone(IST).isoformat(),
        "note": (
            "Nifty 50 spot, India VIX, and full sector breadth are not "
            "available in this build. Treat the 5-stock universe as a proxy."
        ),
        "universe": universe_snapshot,
        "breadth_5stock_proxy": {"up": up, "down": down, "total": len(universe)},
        "open_positions": open_positions_summary or [],
        "realised_pnl_today_inr": round(realised_pnl_today, 2),
        "unrealised_pnl_today_inr": round(unrealised_pnl_today, 2),
        "news_headlines_recent": news_headlines or [],
    }


async def build_pretrade_context(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    signal: SignalT,
    qty: int,
    latest_regime: dict[str, Any] | None,
    news_headlines: list[str] | None = None,
) -> dict[str, Any]:
    async with session_factory() as session:
        last_5_bars = await _last_n_bars(session, signal.symbol, n=5)
    return {
        "signal": {
            "symbol": signal.symbol,
            "direction": signal.direction,
            "entry_price": signal.breakout_price,
            "stop": signal.stop,
            "target": signal.target,
            "or_high": signal.or_high,
            "or_low": signal.or_low,
            "bar_volume": signal.bar_volume,
            "avg_prior_5bar_volume": signal.avg_prior_5bar_volume,
            "volume_ratio": round(signal.volume_ratio, 2),
            "planned_qty": qty,
        },
        "last_5_bars": last_5_bars,
        "latest_regime": latest_regime,
        "symbol_news_last_30min": news_headlines or [],
    }


# ----- DB helpers ------------------------------------------------------------------------


async def _symbol_snapshot(
    session: AsyncSession, symbol: str, today_open_utc: datetime
) -> dict[str, Any]:
    # Today's first bar (or first tick if no bar yet) gives session-open price.
    first_bar = (
        await session.execute(
            select(m.Bar)
            .where(m.Bar.symbol == symbol)
            .where(m.Bar.open_time >= today_open_utc)
            .order_by(m.Bar.open_time.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    last_bar = (
        await session.execute(
            select(m.Bar).where(m.Bar.symbol == symbol).order_by(m.Bar.open_time.desc()).limit(1)
        )
    ).scalar_one_or_none()
    # 5-bar avg volume (excluding the most recent bar)
    recent_bars = (
        (
            await session.execute(
                select(m.Bar)
                .where(m.Bar.symbol == symbol)
                .order_by(m.Bar.open_time.desc())
                .limit(6)
            )
        )
        .scalars()
        .all()
    )
    if last_bar is not None and len(recent_bars) >= 2:
        prior = recent_bars[1:6]
        avg_prior_volume: float | None = sum(b.v for b in prior) / len(prior) if prior else None
    else:
        avg_prior_volume = None

    open_today = first_bar.o if first_bar else None
    current_ltp = last_bar.c if last_bar else None
    pct_change: float | None = None
    if open_today and current_ltp:
        pct_change = round(100.0 * (current_ltp - open_today) / open_today, 3)

    return {
        "symbol": symbol,
        "session_open": open_today,
        "current_ltp": current_ltp,
        "pct_change_today": pct_change,
        "last_bar_volume": last_bar.v if last_bar else None,
        "avg_prior_5bar_volume": (
            round(avg_prior_volume, 1) if avg_prior_volume is not None else None
        ),
    }


async def _last_n_bars(session: AsyncSession, symbol: str, *, n: int) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(m.Bar)
                .where(m.Bar.symbol == symbol)
                .order_by(m.Bar.open_time.desc())
                .limit(n)
            )
        )
        .scalars()
        .all()
    )
    # Return in chronological order
    rows = list(reversed(rows))
    return [
        {
            "open_time_ist": r.open_time.astimezone(IST).isoformat(),
            "o": r.o,
            "h": r.h,
            "l": r.low,
            "c": r.c,
            "v": r.v,
        }
        for r in rows
    ]


async def latest_regime_dict(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_age_minutes: int = 30,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    now = now_utc or datetime.now(UTC)
    cutoff = now - timedelta(minutes=max_age_minutes)
    async with session_factory() as session:
        row = (
            await session.execute(
                select(m.RegimeVerdict)
                .where(m.RegimeVerdict.ts >= cutoff)
                .order_by(m.RegimeVerdict.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "regime": row.regime,
        "confidence": row.confidence,
        "ts_ist": row.ts.astimezone(IST).isoformat(),
        "key_drivers": list(row.key_drivers or []),
        "watch_symbols": list(row.watch_symbols or []),
        "avoid_symbols": list(row.avoid_symbols or []),
        "rationale": row.rationale,
    }
