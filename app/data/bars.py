"""Tick → 1-minute OHLCV bar aggregator.

Bars are aligned to wall-clock UTC minute boundaries. A bar closes when the
*next* tick for that symbol falls into a later minute, so closed bars are
emitted slightly late relative to the minute boundary (latency = inter-tick
gap on that symbol). A `flush_all` helper exists for shutdown / end-of-session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from app.data.types import Bar, Tick

log = structlog.get_logger()


@dataclass(slots=True)
class _BarState:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    first_tv: int
    last_tv: int

    def update(self, tick: Tick) -> None:
        self.high = max(self.high, tick.ltp)
        self.low = min(self.low, tick.ltp)
        self.close = tick.ltp
        # Guard against any non-monotonic total_volume from the feed
        self.last_tv = max(self.last_tv, tick.total_volume)

    def to_bar(self, symbol: str) -> Bar:
        return Bar(
            symbol=symbol,
            open_time=self.open_time,
            close_time=self.open_time + timedelta(minutes=1),
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=max(0, self.last_tv - self.first_tv),
        )


def _bucket_for(ts: datetime) -> datetime:
    """Return the minute-aligned UTC open_time for `ts`."""
    ts_utc = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
    return ts_utc.replace(second=0, microsecond=0)


class BarAggregator:
    """Stateful 1-minute OHLCV aggregator. Single-threaded; ingest from one loop."""

    def __init__(self) -> None:
        self._state: dict[str, _BarState] = {}

    def ingest(self, tick: Tick) -> Bar | None:
        """Feed one tick. Returns a closed bar iff this tick crossed a minute boundary."""
        bucket = _bucket_for(tick.ts)
        state = self._state.get(tick.symbol)

        if state is None:
            self._state[tick.symbol] = _BarState(
                open_time=bucket,
                open=tick.ltp,
                high=tick.ltp,
                low=tick.ltp,
                close=tick.ltp,
                first_tv=tick.total_volume,
                last_tv=tick.total_volume,
            )
            return None

        if bucket == state.open_time:
            state.update(tick)
            return None

        if bucket < state.open_time:
            # Out-of-order tick from a slower symbol clock — fold into current bar
            # but don't move open_time backwards.
            state.update(tick)
            return None

        # bucket > state.open_time → close the existing bar, start a new one
        closed = state.to_bar(tick.symbol)
        self._state[tick.symbol] = _BarState(
            open_time=bucket,
            open=tick.ltp,
            high=tick.ltp,
            low=tick.ltp,
            close=tick.ltp,
            first_tv=tick.total_volume,
            last_tv=tick.total_volume,
        )
        return closed

    def flush_all(self) -> Iterator[Bar]:
        """Emit all in-progress bars; clears state. Use at end-of-session or shutdown."""
        for symbol, state in self._state.items():
            yield state.to_bar(symbol)
        self._state.clear()


async def run_aggregator(
    in_q: asyncio.Queue[Tick],
    out_q: asyncio.Queue[Bar],
    stop: asyncio.Event,
    poll_timeout_s: float = 1.0,
) -> None:
    agg = BarAggregator()
    log.info("bar_aggregator_started")
    try:
        while not stop.is_set():
            try:
                tick = await asyncio.wait_for(in_q.get(), timeout=poll_timeout_s)
            except TimeoutError:
                continue
            closed = agg.ingest(tick)
            if closed is not None:
                await out_q.put(closed)
    finally:
        for bar in agg.flush_all():
            try:
                out_q.put_nowait(bar)
            except asyncio.QueueFull:
                log.warning("bar_flush_queue_full", symbol=bar.symbol)
        log.info("bar_aggregator_stopped")
