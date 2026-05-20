"""Broker abstraction. PaperBroker and (later) AngelOneBroker implement this.

The execution engine drives the broker through two methods: `place_order` to
submit, and `on_tick` so the broker can produce fills (PaperBroker fills against
the next tick; a live broker would receive fills via order-update WebSockets and
buffer them between calls).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.data.types import Tick


@dataclass(frozen=True, slots=True)
class NewOrder:
    """Order request as submitted by the execution engine."""

    symbol: str
    side: str  # "BUY" | "SELL"
    qty: int
    order_type: str = "MARKET"  # "MARKET" | "LIMIT"
    limit_price: float | None = None
    signal_id: int | None = None
    # Reference for slippage tracking — engine's view of "fair" price at decision time.
    ideal_price: float | None = None
    # Set for exit orders so the engine can map a fill back to a position.
    closing_position_id: int | None = None
    # Free-form notes (e.g. "stop_hit", "target_hit", "time_stop") for the journal.
    role: str = "entry"


@dataclass(frozen=True, slots=True)
class FillResult:
    """In-memory fill emitted by the broker. Persisted by the engine."""

    order_id: int  # local journal Order.id
    broker_order_id: str
    symbol: str
    side: str
    qty: int
    price: float
    charges_inr: float
    ts: datetime
    ideal_price: float | None
    role: str
    closing_position_id: int | None
    signal_id: int | None
    extras: dict[str, Any] = field(default_factory=dict)


class BrokerInterface(ABC):
    """Stateful broker adapter; instantiated once per session."""

    @abstractmethod
    async def place_order(self, order: NewOrder) -> int:
        """Persist the order and return its local journal Order.id.

        For PaperBroker, this records the order and waits for the next tick to
        fill it. For a live broker, this submits to the upstream and stores the
        broker-issued order id alongside our local id.
        """

    @abstractmethod
    async def on_tick(self, tick: Tick) -> list[FillResult]:
        """Process the latest tick; return any fills that this tick triggered."""

    @abstractmethod
    def has_pending_orders(self) -> bool:
        """True if any orders are still waiting to fill (for shutdown safety)."""
