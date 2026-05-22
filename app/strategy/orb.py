"""Opening Range Breakout (ORB) strategy — pure logic, no I/O.

Per-symbol state machine:

  IDLE → first bar in [09:15, 09:30) IST → BUILDING_OR
  BUILDING_OR → tracks or_high / or_low across the 15 OR bars
  OR locks at the first bar with open_time >= 09:30 IST
  Post-OR bars are evaluated for breakout:
    long  if bar.close > or_high  AND bar.volume > vol_multiplier * mean(prior 5)
    short if bar.close < or_low   AND bar.volume > vol_multiplier * mean(prior 5)
  One signal per symbol per day. Day reset is automatic on bar.open_time date change.

Stops and targets:
    long:  stop = or_low,  target = entry + 1.5 * (entry - or_low)
    short: stop = or_high, target = entry - 1.5 * (or_high - entry)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime

import structlog

from app.config import IST
from app.data.types import Bar, Signal

log = structlog.get_logger()

OR_START_HOUR_IST = 9
OR_START_MIN_IST = 15  # Market open

DEFAULT_OR_WINDOW_MINUTES = 15  # OR ends at 09:30 IST by default
DEFAULT_VOLUME_MULTIPLIER = 1.5
DEFAULT_VOLUME_LOOKBACK = 5
DEFAULT_TARGET_R_MULTIPLE = 1.5


@dataclass(slots=True)
class _SymbolState:
    day: date | None = None
    or_high: float | None = None
    or_low: float | None = None
    or_bars_seen: int = 0
    or_locked: bool = False
    signaled: bool = False
    recent_volumes: deque[int] = field(
        default_factory=lambda: deque(maxlen=DEFAULT_VOLUME_LOOKBACK)
    )

    def reset(self, day: date) -> None:
        self.day = day
        self.or_high = None
        self.or_low = None
        self.or_bars_seen = 0
        self.or_locked = False
        self.signaled = False
        self.recent_volumes.clear()


def _or_end_hm(or_window_minutes: int) -> tuple[int, int]:
    """Compute the OR-end (hour, minute) for a given OR window length."""
    total = OR_START_HOUR_IST * 60 + OR_START_MIN_IST + or_window_minutes
    return (total // 60, total % 60)


def _is_in_or_window(open_time_ist: datetime, or_end_hm: tuple[int, int]) -> bool:
    h, m = open_time_ist.hour, open_time_ist.minute
    after_start = (h, m) >= (OR_START_HOUR_IST, OR_START_MIN_IST)
    before_end = (h, m) < or_end_hm
    return after_start and before_end


def _is_post_or(open_time_ist: datetime, or_end_hm: tuple[int, int]) -> bool:
    return (open_time_ist.hour, open_time_ist.minute) >= or_end_hm


class ORBStrategy:
    """Stateful, single-threaded; drive it with `on_bar(bar)` per closed bar."""

    def __init__(
        self,
        *,
        or_window_minutes: int = DEFAULT_OR_WINDOW_MINUTES,
        volume_multiplier: float = DEFAULT_VOLUME_MULTIPLIER,
        volume_lookback: int = DEFAULT_VOLUME_LOOKBACK,
        target_r_multiple: float = DEFAULT_TARGET_R_MULTIPLE,
    ) -> None:
        self._or_window_minutes = or_window_minutes
        self._or_end_hm = _or_end_hm(or_window_minutes)
        self._vol_mult = volume_multiplier
        self._vol_lookback = volume_lookback
        self._target_r = target_r_multiple
        self._state: dict[str, _SymbolState] = {}

    def on_bar(self, bar: Bar) -> Signal | None:
        """Process one closed bar. Returns a Signal iff this bar triggered a breakout."""
        open_ist = bar.open_time.astimezone(IST)
        day = open_ist.date()
        state = self._state.setdefault(bar.symbol, _SymbolState())

        if state.day != day:
            state.reset(day)

        if _is_in_or_window(open_ist, self._or_end_hm):
            self._update_or(state, bar)
            state.recent_volumes.append(bar.volume)
            return None

        if not _is_post_or(open_ist, self._or_end_hm):
            # Bar is before 09:15 IST — pre-market or auction; ignore.
            return None

        # Post-OR territory. Lock the OR on first encounter; emit a one-time log.
        if not state.or_locked:
            state.or_locked = True
            if state.or_bars_seen == 0:
                log.warning(
                    "orb_no_or_bars_skipping_day",
                    symbol=bar.symbol,
                    day=str(day),
                )
            else:
                log.info(
                    "orb_locked",
                    symbol=bar.symbol,
                    or_high=state.or_high,
                    or_low=state.or_low,
                    or_bars=state.or_bars_seen,
                )

        signal = self._maybe_signal(state, bar)
        state.recent_volumes.append(bar.volume)
        return signal

    def _update_or(self, state: _SymbolState, bar: Bar) -> None:
        if state.or_high is None or state.or_low is None:
            state.or_high = bar.high
            state.or_low = bar.low
        else:
            state.or_high = max(state.or_high, bar.high)
            state.or_low = min(state.or_low, bar.low)
        state.or_bars_seen += 1

    def _maybe_signal(self, state: _SymbolState, bar: Bar) -> Signal | None:
        if state.signaled:
            return None
        if state.or_high is None or state.or_low is None:
            return None
        if len(state.recent_volumes) < self._vol_lookback:
            return None

        avg_vol = sum(state.recent_volumes) / len(state.recent_volumes)
        if avg_vol <= 0:
            return None
        vol_ratio = bar.volume / avg_vol
        if vol_ratio <= self._vol_mult:
            return None

        direction: str | None = None
        if bar.close > state.or_high:
            direction = "long"
        elif bar.close < state.or_low:
            direction = "short"
        if direction is None:
            return None

        if direction == "long":
            stop = state.or_low
            target = bar.close + self._target_r * (bar.close - stop)
        else:
            stop = state.or_high
            target = bar.close - self._target_r * (stop - bar.close)

        state.signaled = True
        return Signal(
            symbol=bar.symbol,
            direction=direction,
            breakout_close_time=bar.close_time,
            breakout_price=bar.close,
            or_high=state.or_high,
            or_low=state.or_low,
            stop=stop,
            target=target,
            bar_volume=bar.volume,
            avg_prior_5bar_volume=avg_vol,
            volume_ratio=vol_ratio,
        )
