"""Execution engine — turns Signals into paper trades and manages their lifecycle.

Flow:
  Signal → on_signal:
      risk.check_all → if blocked, log to risk_blocks (Signal stays NEW; not persisted)
      sizing.size_position → qty (or block reason)
      persist Signal row (status=SUBMITTED, qty/stop/target locked)
      broker.place_order(entry NewOrder)

  Tick → on_tick:
      broker.on_tick → list[FillResult]
      _apply_fill for each
      _check_exits for any open position whose symbol matches tick.symbol or
        for time-stop (>= 15:15 IST)
      broker.place_order for any exit decisions

The engine is the single owner of in-memory position book + realised P&L
counters; the broker only tracks pending orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.brokers.base import BrokerInterface, FillResult, NewOrder
from app.config import IST, get_settings
from app.data.types import Signal as SignalT
from app.data.types import Tick
from app.journal import models as m
from app.risk.limits import EngineSnapshot, RiskBlock, check_all
from app.risk.sizing import size_position

log = structlog.get_logger()

TIME_STOP_IST = time(15, 15)


@dataclass(slots=True)
class _OpenPosition:
    position_id: int
    symbol: str
    direction: str  # "long" | "short"
    qty: int
    entry_price: float
    stop: float
    target: float
    entry_charges: float
    entry_signal_id: int
    opened_at: datetime
    exit_order_pending: bool = False


class ExecutionEngine:
    def __init__(
        self,
        broker: BrokerInterface,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._broker = broker
        self._sf = session_factory
        settings = get_settings()
        self._capital = settings.capital_inr
        self._daily_loss_limit = settings.daily_loss_limit_inr
        self._weekly_loss_limit = settings.weekly_loss_limit_inr
        self._risk_pct = settings.risk_per_trade_pct
        self._max_trades_per_day = settings.max_trades_per_day

        self._open: dict[int, _OpenPosition] = {}
        self._open_by_symbol: dict[str, int] = {}
        self._trades_today: int = 0
        self._stopped_out_today: set[str] = set()
        self._realised_today: float = 0.0
        self._realised_week: float = 0.0
        self._unrealised_today: float = 0.0  # refreshed on every tick

    # ----- public API used by orchestrator -------------------------------------------------

    def restore_pnl_state(self, *, realised_today: float, realised_week: float) -> None:
        """Seed the engine's realised counters from the DB on startup."""
        self._realised_today = realised_today
        self._realised_week = realised_week

    async def on_signal(self, signal: SignalT) -> int | None:
        """Risk-check, size, and place an entry order. Returns Signal.id or None."""
        now = signal.breakout_close_time
        snapshot = self._snapshot(now)

        block = check_all(
            snapshot=snapshot,
            symbol=signal.symbol,
            daily_loss_limit_inr=self._daily_loss_limit,
            weekly_loss_limit_inr=self._weekly_loss_limit,
            max_trades_per_day=self._max_trades_per_day,
        )
        if block is not None:
            await self._log_risk_block(block, signal=signal)
            return None

        available = (
            self._capital
            + self._realised_today
            - sum(p.qty * p.entry_price for p in self._open.values())
        )
        sizing = size_position(
            capital_inr=self._capital,
            available_capital_inr=available,
            risk_pct=self._risk_pct,
            entry=signal.breakout_price,
            stop=signal.stop,
        )
        if not sizing.accepted:
            await self._log_risk_block(
                RiskBlock(
                    reason=sizing.rejected_reason or "sizing_failed", detail={"qty": sizing.qty}
                ),
                signal=signal,
            )
            return None

        signal_id = await self._persist_signal(signal, qty=sizing.qty, status="SUBMITTED")
        side = "BUY" if signal.direction == "long" else "SELL"
        order = NewOrder(
            symbol=signal.symbol,
            side=side,
            qty=sizing.qty,
            order_type="MARKET",
            signal_id=signal_id,
            ideal_price=signal.breakout_price,
            role="entry",
        )
        await self._broker.place_order(order)
        self._trades_today += 1
        log.info(
            "engine_entry_submitted",
            signal_id=signal_id,
            symbol=signal.symbol,
            side=side,
            qty=sizing.qty,
            stop=signal.stop,
            target=signal.target,
            trades_today=self._trades_today,
        )
        return signal_id

    async def on_tick(self, tick: Tick) -> None:
        fills = await self._broker.on_tick(tick)
        for fill in fills:
            await self._apply_fill(fill)
        await self._check_exits(tick)
        self._refresh_unrealised(tick)

    # ----- internals -----------------------------------------------------------------------

    def _snapshot(self, now_utc: datetime) -> EngineSnapshot:
        return EngineSnapshot(
            now_utc=now_utc,
            open_position_count=len(self._open),
            open_position_symbols=frozenset(self._open_by_symbol.keys()),
            trades_today=self._trades_today,
            realised_pnl_today=self._realised_today,
            unrealised_pnl_today=self._unrealised_today,
            realised_pnl_week=self._realised_week,
            stopped_out_symbols_today=frozenset(self._stopped_out_today),
        )

    async def _apply_fill(self, fill: FillResult) -> None:
        if fill.role == "entry":
            await self._open_position(fill)
        else:
            await self._close_position(fill)

    async def _open_position(self, fill: FillResult) -> None:
        assert fill.signal_id is not None
        async with self._sf() as session:
            sig = await session.get(m.Signal, fill.signal_id)
            assert sig is not None
            sig.status = "FILLED"
            pos = m.Position(
                symbol=fill.symbol,
                qty=fill.qty if sig.direction == "long" else -fill.qty,
                avg_entry=fill.price,
                opened_at=fill.ts,
                realised_pnl=0.0,
            )
            session.add(pos)
            await session.commit()
            await session.refresh(pos)
            stop = sig.stop
            target = sig.target
            direction = sig.direction
            position_id = pos.id
            signal_id = sig.id

        self._open[position_id] = _OpenPosition(
            position_id=position_id,
            symbol=fill.symbol,
            direction=direction,
            qty=fill.qty,
            entry_price=fill.price,
            stop=stop,
            target=target,
            entry_charges=fill.charges_inr,
            entry_signal_id=signal_id,
            opened_at=fill.ts,
        )
        self._open_by_symbol[fill.symbol] = position_id
        log.info(
            "engine_position_opened",
            position_id=position_id,
            symbol=fill.symbol,
            direction=direction,
            qty=fill.qty,
            entry=round(fill.price, 4),
            stop=round(stop, 4),
            target=round(target, 4),
            entry_charges=round(fill.charges_inr, 2),
        )

    async def _close_position(self, fill: FillResult) -> None:
        assert fill.closing_position_id is not None
        pos = self._open.pop(fill.closing_position_id, None)
        if pos is None:
            log.warning("exit_fill_for_unknown_position", order_id=fill.order_id)
            return
        self._open_by_symbol.pop(pos.symbol, None)

        if pos.direction == "long":
            gross = (fill.price - pos.entry_price) * pos.qty
        else:
            gross = (pos.entry_price - fill.price) * pos.qty
        total_charges = pos.entry_charges + fill.charges_inr
        net = gross - total_charges

        async with self._sf() as session:
            row = await session.get(m.Position, pos.position_id)
            assert row is not None
            row.closed_at = fill.ts
            row.realised_pnl = net
            await session.commit()

        self._realised_today += net
        self._realised_week += net
        if fill.role == "stop_hit":
            self._stopped_out_today.add(pos.symbol)

        log.info(
            "engine_position_closed",
            position_id=pos.position_id,
            symbol=pos.symbol,
            direction=pos.direction,
            qty=pos.qty,
            entry=round(pos.entry_price, 4),
            exit=round(fill.price, 4),
            gross=round(gross, 2),
            charges=round(total_charges, 2),
            net=round(net, 2),
            reason=fill.role,
        )

    async def _check_exits(self, tick: Tick) -> None:
        position_id = self._open_by_symbol.get(tick.symbol)
        time_stop_active = self._time_stop_active(tick.ts)

        # Time stop fires across all open positions, not just tick.symbol's
        for pid, pos in list(self._open.items()):
            if pos.exit_order_pending:
                continue
            if time_stop_active and pid != position_id:
                # We'll only emit time-stop exit when a tick arrives for that
                # position's symbol — otherwise we don't have a price to fill at.
                continue

        # The symbol-specific exits use tick.ltp
        if position_id is None:
            return
        pos = self._open[position_id]
        if pos.exit_order_pending:
            return

        reason: str | None = None
        if time_stop_active:
            reason = "time_stop"
        elif pos.direction == "long":
            if tick.ltp <= pos.stop:
                reason = "stop_hit"
            elif tick.ltp >= pos.target:
                reason = "target_hit"
        else:  # short
            if tick.ltp >= pos.stop:
                reason = "stop_hit"
            elif tick.ltp <= pos.target:
                reason = "target_hit"
        if reason is None:
            return

        side = "SELL" if pos.direction == "long" else "BUY"
        order = NewOrder(
            symbol=pos.symbol,
            side=side,
            qty=pos.qty,
            order_type="MARKET",
            ideal_price=tick.ltp,
            role=reason,
            closing_position_id=pos.position_id,
        )
        await self._broker.place_order(order)
        pos.exit_order_pending = True
        log.info(
            "engine_exit_submitted",
            position_id=pos.position_id,
            symbol=pos.symbol,
            reason=reason,
            ltp_trigger=round(tick.ltp, 4),
        )

    def _time_stop_active(self, now_utc: datetime) -> bool:
        return now_utc.astimezone(IST).time() >= TIME_STOP_IST

    def _refresh_unrealised(self, tick: Tick) -> None:
        # Cheap pass over the (at-most-2) open positions to update unrealised.
        total = 0.0
        for pos in self._open.values():
            if pos.symbol != tick.symbol:
                # We approximate with the last entry-time price; only the
                # position matching the current tick gets a fresh mark.
                continue
            if pos.direction == "long":
                total += (tick.ltp - pos.entry_price) * pos.qty
            else:
                total += (pos.entry_price - tick.ltp) * pos.qty
        self._unrealised_today = total

    async def _persist_signal(self, signal: SignalT, *, qty: int, status: str) -> int:
        async with self._sf() as session:
            row = m.Signal(
                symbol=signal.symbol,
                direction=signal.direction,
                breakout_price=signal.breakout_price,
                or_high=signal.or_high,
                or_low=signal.or_low,
                qty=qty,
                stop=signal.stop,
                target=signal.target,
                status=status,
                created_at=signal.breakout_close_time,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return int(row.id)

    async def _log_risk_block(self, block: RiskBlock, *, signal: SignalT) -> None:
        async with self._sf() as session:
            session.add(
                m.RiskBlock(
                    ts=datetime.now(UTC),
                    reason=block.reason,
                    signal_id=None,
                    payload={"symbol": signal.symbol, **block.detail},
                )
            )
            await session.commit()
        log.warning(
            "risk_block",
            reason=block.reason,
            symbol=signal.symbol,
            **{k: v for k, v in block.detail.items() if isinstance(v, (str, int, float, bool))},
        )


# ----- helpers used by the orchestrator at startup --------------------------------


async def initial_realised_pnl(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now_utc: datetime | None = None,
) -> tuple[float, float]:
    """Sum realised P&L for today and the current ISO week from closed positions.

    Used by the engine on startup so a mid-day restart picks up correct counters.
    Returns (today, this_week).
    """
    now = (now_utc or datetime.now(UTC)).astimezone(IST)
    today_ist_start = datetime.combine(now.date(), time(0, 0), tzinfo=IST).astimezone(UTC)
    week_start_ist = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start_utc = week_start_ist.astimezone(UTC)

    async with session_factory() as session:
        today = (
            await session.execute(
                select(func.coalesce(func.sum(m.Position.realised_pnl), 0.0))
                .where(m.Position.closed_at.is_not(None))
                .where(m.Position.closed_at >= today_ist_start)
            )
        ).scalar_one()
        week = (
            await session.execute(
                select(func.coalesce(func.sum(m.Position.realised_pnl), 0.0))
                .where(m.Position.closed_at.is_not(None))
                .where(m.Position.closed_at >= week_start_utc)
            )
        ).scalar_one()
    return float(today), float(week)
