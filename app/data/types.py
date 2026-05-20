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
