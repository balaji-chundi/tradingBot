from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Tick:
    """One market data update for a single symbol.

    `ltp` is in INR (the broker reports paise; the feed adapter divides by 100
    before constructing the Tick). `total_volume` is the cumulative traded
    quantity for the symbol on the current session — bar volume is computed as
    a delta in [[bars-aggregator]].
    """

    symbol: str
    token: str
    ltp: float
    ltq: int
    total_volume: int
    ts: datetime
    exchange_ts_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Bar:
    """A 1-minute OHLCV bar aligned to the minute boundary in UTC."""

    symbol: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    interval: str = "1m"


@dataclass(frozen=True, slots=True)
class Signal:
    """ORB breakout candidate emitted by the strategy layer.

    Phase 2 produces these from closed bars; Phase 3 risk-sizes them into
    Orders (or rejects them via [[risk-limits]]). `breakout_price` is the close
    of the breakout bar — the actual fill price comes from the broker adapter
    on the next tick.
    """

    symbol: str
    direction: str  # "long" | "short"
    breakout_close_time: datetime
    breakout_price: float
    or_high: float
    or_low: float
    stop: float
    target: float
    bar_volume: int
    avg_prior_5bar_volume: float
    volume_ratio: float
