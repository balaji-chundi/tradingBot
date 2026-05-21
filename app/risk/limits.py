"""Risk checks that gate every new signal before it becomes an order.

Each check returns a `RiskBlock` if it failed, or None if it passed. The engine
walks them in priority order and uses the first failure (so the most relevant
reason is logged). Failures are persisted to the `risk_blocks` table for
end-of-day review.

Section 6 of the brief:
    * Daily realised + unrealised P&L ≤ -₹1,500
    * Weekly realised P&L ≤ -₹3,000
    * Open positions count ≥ 2
    * Time outside 09:30-14:45 IST (no late entries)
    * WebSocket has been disconnected within last 60 seconds   (Phase 6)
    * LLM regime risk_off with confidence > 0.7 + respect_regime=True
Plus from Section 5:
    * Max trades today ≥ 2
    * Re-entry on a stopped-out symbol disallowed
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from app.config import IST

TRADING_ENTRY_START = time(9, 30)
TRADING_ENTRY_END = time(14, 45)


@dataclass(frozen=True, slots=True)
class RiskBlock:
    reason: str
    detail: dict[str, object]


@dataclass(slots=True)
class EngineSnapshot:
    """Read-only view of engine state passed into risk checks."""

    now_utc: datetime
    open_position_count: int
    open_position_symbols: frozenset[str]
    trades_today: int
    realised_pnl_today: float
    unrealised_pnl_today: float
    realised_pnl_week: float
    stopped_out_symbols_today: frozenset[str]


def _in_entry_window(now_utc: datetime) -> bool:
    ist = now_utc.astimezone(IST).time()
    return TRADING_ENTRY_START <= ist < TRADING_ENTRY_END


REGIME_RISK_OFF_CONFIDENCE_THRESHOLD = 0.7
FEED_STALE_SECONDS = 60.0


def check_all(
    *,
    snapshot: EngineSnapshot,
    symbol: str,
    daily_loss_limit_inr: float,
    weekly_loss_limit_inr: float,
    max_trades_per_day: int,
    latest_regime_label: str | None = None,
    latest_regime_confidence: float | None = None,
    respect_regime: bool = True,
    feed_age_s: float | None = None,
    kill_switch_active: bool = False,
) -> RiskBlock | None:
    if kill_switch_active:
        return RiskBlock(reason="kill_switch", detail={})

    if feed_age_s is not None and feed_age_s > FEED_STALE_SECONDS:
        return RiskBlock(
            reason="feed_stale",
            detail={"age_s": round(feed_age_s, 1), "limit_s": FEED_STALE_SECONDS},
        )

    if not _in_entry_window(snapshot.now_utc):
        return RiskBlock(
            reason="outside_entry_window",
            detail={
                "ist_now": snapshot.now_utc.astimezone(IST).isoformat(),
                "window": f"{TRADING_ENTRY_START}-{TRADING_ENTRY_END} IST",
            },
        )

    if snapshot.trades_today >= max_trades_per_day:
        return RiskBlock(
            reason="max_trades_per_day_reached",
            detail={"trades_today": snapshot.trades_today, "limit": max_trades_per_day},
        )

    if snapshot.open_position_count >= 2:
        return RiskBlock(
            reason="max_open_positions",
            detail={"open": snapshot.open_position_count},
        )

    if symbol in snapshot.open_position_symbols:
        return RiskBlock(
            reason="already_open_in_symbol",
            detail={"symbol": symbol},
        )

    if symbol in snapshot.stopped_out_symbols_today:
        return RiskBlock(
            reason="reentry_after_stopout_blocked",
            detail={"symbol": symbol},
        )

    daily_total = snapshot.realised_pnl_today + snapshot.unrealised_pnl_today
    if daily_total <= -daily_loss_limit_inr:
        return RiskBlock(
            reason="daily_loss_limit_hit",
            detail={"pnl_today": daily_total, "limit": -daily_loss_limit_inr},
        )

    if snapshot.realised_pnl_week <= -weekly_loss_limit_inr:
        return RiskBlock(
            reason="weekly_loss_limit_hit",
            detail={
                "realised_week": snapshot.realised_pnl_week,
                "limit": -weekly_loss_limit_inr,
            },
        )

    if (
        respect_regime
        and latest_regime_label == "risk_off"
        and latest_regime_confidence is not None
        and latest_regime_confidence > REGIME_RISK_OFF_CONFIDENCE_THRESHOLD
    ):
        return RiskBlock(
            reason="regime_risk_off",
            detail={
                "confidence": latest_regime_confidence,
                "threshold": REGIME_RISK_OFF_CONFIDENCE_THRESHOLD,
            },
        )

    return None
