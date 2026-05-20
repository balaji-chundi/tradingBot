from __future__ import annotations

from datetime import UTC, datetime

from app.config import IST
from app.risk.limits import EngineSnapshot, check_all


def _ist(h: int, m: int) -> datetime:
    return datetime(2026, 5, 20, h, m, tzinfo=IST).astimezone(UTC)


def _snap(
    *,
    now: datetime,
    open_count: int = 0,
    open_symbols: frozenset[str] = frozenset(),
    trades_today: int = 0,
    realised: float = 0.0,
    unrealised: float = 0.0,
    realised_week: float = 0.0,
    stopped_out: frozenset[str] = frozenset(),
) -> EngineSnapshot:
    return EngineSnapshot(
        now_utc=now,
        open_position_count=open_count,
        open_position_symbols=open_symbols,
        trades_today=trades_today,
        realised_pnl_today=realised,
        unrealised_pnl_today=unrealised,
        realised_pnl_week=realised_week,
        stopped_out_symbols_today=stopped_out,
    )


COMMON = dict(daily_loss_limit_inr=1500.0, weekly_loss_limit_inr=3000.0, max_trades_per_day=2)


def test_passes_in_window_with_clean_state() -> None:
    block = check_all(snapshot=_snap(now=_ist(10, 30)), symbol="RELIANCE-EQ", **COMMON)
    assert block is None


def test_blocked_before_0930_ist() -> None:
    block = check_all(snapshot=_snap(now=_ist(9, 29)), symbol="RELIANCE-EQ", **COMMON)
    assert block is not None
    assert block.reason == "outside_entry_window"


def test_blocked_after_1445_ist() -> None:
    block = check_all(snapshot=_snap(now=_ist(14, 46)), symbol="RELIANCE-EQ", **COMMON)
    assert block is not None
    assert block.reason == "outside_entry_window"


def test_max_trades_per_day() -> None:
    block = check_all(
        snapshot=_snap(now=_ist(10, 30), trades_today=2),
        symbol="RELIANCE-EQ",
        **COMMON,
    )
    assert block is not None
    assert block.reason == "max_trades_per_day_reached"


def test_max_open_positions() -> None:
    block = check_all(
        snapshot=_snap(now=_ist(10, 30), open_count=2),
        symbol="RELIANCE-EQ",
        **COMMON,
    )
    assert block is not None
    assert block.reason == "max_open_positions"


def test_already_open_in_same_symbol() -> None:
    block = check_all(
        snapshot=_snap(now=_ist(10, 30), open_count=1, open_symbols=frozenset({"RELIANCE-EQ"})),
        symbol="RELIANCE-EQ",
        **COMMON,
    )
    assert block is not None
    assert block.reason == "already_open_in_symbol"


def test_reentry_after_stopout_blocked() -> None:
    block = check_all(
        snapshot=_snap(now=_ist(10, 30), stopped_out=frozenset({"RELIANCE-EQ"})),
        symbol="RELIANCE-EQ",
        **COMMON,
    )
    assert block is not None
    assert block.reason == "reentry_after_stopout_blocked"


def test_daily_loss_limit_hit() -> None:
    # realised -800 + unrealised -800 = -1600 ≤ -1500
    block = check_all(
        snapshot=_snap(now=_ist(10, 30), realised=-800, unrealised=-800),
        symbol="RELIANCE-EQ",
        **COMMON,
    )
    assert block is not None
    assert block.reason == "daily_loss_limit_hit"


def test_weekly_loss_limit_hit() -> None:
    block = check_all(
        snapshot=_snap(now=_ist(10, 30), realised_week=-3100),
        symbol="RELIANCE-EQ",
        **COMMON,
    )
    assert block is not None
    assert block.reason == "weekly_loss_limit_hit"


def test_regime_risk_off_high_confidence_blocks() -> None:
    block = check_all(
        snapshot=_snap(now=_ist(10, 30)),
        symbol="RELIANCE-EQ",
        latest_regime_label="risk_off",
        latest_regime_confidence=0.85,
        respect_regime=True,
        **COMMON,
    )
    assert block is not None
    assert block.reason == "regime_risk_off"


def test_regime_risk_off_low_confidence_does_not_block() -> None:
    block = check_all(
        snapshot=_snap(now=_ist(10, 30)),
        symbol="RELIANCE-EQ",
        latest_regime_label="risk_off",
        latest_regime_confidence=0.5,
        respect_regime=True,
        **COMMON,
    )
    assert block is None


def test_regime_check_disabled_when_respect_regime_false() -> None:
    block = check_all(
        snapshot=_snap(now=_ist(10, 30)),
        symbol="RELIANCE-EQ",
        latest_regime_label="risk_off",
        latest_regime_confidence=0.99,
        respect_regime=False,
        **COMMON,
    )
    assert block is None


def test_regime_check_passes_when_no_verdict() -> None:
    block = check_all(
        snapshot=_snap(now=_ist(10, 30)),
        symbol="RELIANCE-EQ",
        latest_regime_label=None,
        latest_regime_confidence=None,
        respect_regime=True,
        **COMMON,
    )
    assert block is None
