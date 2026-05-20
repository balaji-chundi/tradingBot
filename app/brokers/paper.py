"""PaperBroker: simulates fills against the next incoming tick.

Per brief Section 8: an order placed at decision time stays PENDING until the
next tick for that symbol, then fills at `tick.ltp ± slippage` (5 bps for
liquid Nifty 50 names). Charges are deducted using the same NSE intraday
formula a live trade would incur.

The broker maintains:
  * a per-symbol queue of pending orders (FIFO)
  * a monotonic local order_id counter (paper has no upstream id; we reuse the
    journal Order.id as both `order_id` and `broker_order_id`)

It does NOT maintain a position book — that lives in the execution engine.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.brokers.base import BrokerInterface, FillResult, NewOrder
from app.data.types import Tick
from app.journal import models as m
from app.journal.charges import charges_for_leg

log = structlog.get_logger()

DEFAULT_SLIPPAGE_BPS = 5.0  # 0.05% one-way, conservative for liquid Nifty 50


class PaperBroker(BrokerInterface):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    ) -> None:
        self._sf = session_factory
        self._slip = slippage_bps / 10_000.0  # bps → fractional
        self._pending: dict[str, deque[tuple[int, NewOrder]]] = defaultdict(deque)
        self._pending_count = 0

    async def place_order(self, order: NewOrder) -> int:
        async with self._sf() as session:
            row = m.Order(
                signal_id=order.signal_id,
                broker_order_id=None,  # set on fill
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                order_type=order.order_type,
                limit_price=order.limit_price,
                status="PENDING",
                created_at=datetime.now(UTC),
                payload={
                    "role": order.role,
                    "closing_position_id": order.closing_position_id,
                    "ideal_price": order.ideal_price,
                },
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            order_id = row.id
        self._pending[order.symbol].append((order_id, order))
        self._pending_count += 1
        log.info(
            "paper_order_placed",
            order_id=order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            role=order.role,
        )
        return order_id

    async def on_tick(self, tick: Tick) -> list[FillResult]:
        queue = self._pending.get(tick.symbol)
        if not queue:
            return []

        fills: list[FillResult] = []
        # Drain the entire queue for this symbol on this tick.
        while queue:
            order_id, order = queue.popleft()
            self._pending_count -= 1
            fill = await self._fill(order_id, order, tick)
            fills.append(fill)
        return fills

    def has_pending_orders(self) -> bool:
        return self._pending_count > 0

    async def _fill(self, order_id: int, order: NewOrder, tick: Tick) -> FillResult:
        ideal = order.ideal_price if order.ideal_price is not None else tick.ltp
        if order.side == "BUY":
            fill_price = tick.ltp * (1.0 + self._slip)
        else:
            fill_price = tick.ltp * (1.0 - self._slip)
        charges = charges_for_leg(side=order.side, qty=order.qty, price=fill_price)
        now = datetime.now(UTC)

        async with self._sf() as session:
            order_row = await session.get(m.Order, order_id)
            assert order_row is not None
            order_row.status = "FILLED"
            order_row.broker_order_id = f"PAPER-{order_id}"
            order_row.submitted_at = now
            session.add(
                m.Fill(
                    order_id=order_id,
                    qty=order.qty,
                    price=fill_price,
                    charges_inr=charges,
                    ts=now,
                )
            )
            slip_bps = 10_000.0 * (fill_price - ideal) / ideal if ideal > 0 else 0.0
            session.add(
                m.SlippageLog(
                    order_id=order_id,
                    ideal_price=ideal,
                    simulated_price=fill_price,
                    slippage_bps=slip_bps,
                    ts=now,
                )
            )
            await session.commit()

        log.info(
            "paper_filled",
            order_id=order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            ideal=round(ideal, 4),
            fill=round(fill_price, 4),
            charges=round(charges, 2),
            role=order.role,
        )

        return FillResult(
            order_id=order_id,
            broker_order_id=f"PAPER-{order_id}",
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=fill_price,
            charges_inr=charges,
            ts=now,
            ideal_price=ideal,
            role=order.role,
            closing_position_id=order.closing_position_id,
            signal_id=order.signal_id,
        )
