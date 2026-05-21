"""Phase-6 additions to risk/limits: kill_switch + feed_stale."""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import IST
from app.risk.limits import EngineSnapshot, check_all


def _ist(h: int, m: int) -> datetime:
    return datetime(2026, 5, 21, h, m, tzinfo=IST).astimezone(UTC)


def _snap(now: datetime) -> EngineSnapshot:
    return EngineSnapshot(
        now_utc=now,
        open_position_count=0,
        open_position_symbols=frozenset(),
        trades_today=0,
        realised_pnl_today=0.0,
        unrealised_pnl_today=0.0,
        realised_pnl_week=0.0,
        stopped_out_symbols_today=frozenset(),
    )


COMMON = dict(daily_loss_limit_inr=1500.0, weekly_loss_limit_inr=3000.0, max_trades_per_day=2)


def test_kill_switch_active_blocks_first() -> None:
    block = check_all(
        snapshot=_snap(_ist(10, 30)),
        symbol="RELIANCE-EQ",
        kill_switch_active=True,
        **COMMON,
    )
    assert block is not None
    assert block.reason == "kill_switch"


def test_feed_age_under_threshold_passes() -> None:
    block = check_all(
        snapshot=_snap(_ist(10, 30)),
        symbol="RELIANCE-EQ",
        feed_age_s=10.0,
        **COMMON,
    )
    assert block is None


def test_feed_age_over_threshold_blocks() -> None:
    block = check_all(
        snapshot=_snap(_ist(10, 30)),
        symbol="RELIANCE-EQ",
        feed_age_s=90.0,
        **COMMON,
    )
    assert block is not None
    assert block.reason == "feed_stale"
    assert block.detail["age_s"] == 90.0


def test_kill_switch_takes_precedence_over_other_blocks() -> None:
    # Even with conditions that would otherwise block (outside window),
    # kill_switch is reported first because we check it before anything.
    block = check_all(
        snapshot=_snap(_ist(8, 0)),  # before entry window
        symbol="RELIANCE-EQ",
        kill_switch_active=True,
        **COMMON,
    )
    assert block is not None
    assert block.reason == "kill_switch"
