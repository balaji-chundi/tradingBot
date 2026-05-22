"""Replay engine — feeds historical 1-min bars through the real ORBStrategy
and a deterministic fill simulator. Same gates as the live engine apply
EXCEPT the LLM ones (regime / pretrade), which can't be replayed faithfully.

Fill model:
  - Entry: at the NEXT bar's open, ± 5 bps slippage.
  - Exit: same bar's stop/target if bar.low/high crossed it; pessimistic on
    "both sides hit in the same minute" → assume stop hit first.
  - Time stop: on the first bar with open_time_ist >= 15:15, exit at that
    bar's open ± slippage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from datetime import time as dtime

import structlog

from app.config import IST, get_settings
from app.data.types import Bar
from app.journal.charges import charges_for_leg
from app.risk.sizing import size_position
from app.strategy.orb import ORBStrategy

log = structlog.get_logger()

SLIPPAGE_BPS = 5.0
SLIP = SLIPPAGE_BPS / 10_000.0
MAX_TRADES_PER_DAY = 2
TIME_STOP_IST = dtime(15, 15)


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    date_ist: str
    symbol: str
    direction: str  # "long" | "short"
    qty: int
    entry_time_ist: str
    entry_price: float
    entry_charges: float
    exit_time_ist: str
    exit_price: float
    exit_charges: float
    exit_reason: str  # "target_hit" | "stop_hit" | "time_stop"
    stop: float
    target: float
    gross_pnl: float
    net_pnl: float
    r_multiple: float


@dataclass(frozen=True, slots=True)
class BacktestSession:
    date_ist: str
    signals_fired: int
    signals_blocked_by_max_trades: int
    signals_blocked_by_reentry: int
    signals_blocked_by_already_open: int
    signals_blocked_by_sizing: int
    trades_taken: int
    net_pnl_day: float


@dataclass(slots=True)
class BacktestResult:
    start_ist: str
    end_ist: str
    capital_inr: float
    trades: list[BacktestTrade] = field(default_factory=list)
    sessions: list[BacktestSession] = field(default_factory=list)


@dataclass(slots=True)
class _OpenPos:
    symbol: str
    direction: str
    qty: int
    entry_price: float
    entry_charges: float
    entry_time_ist: str
    stop: float
    target: float
    initial_risk_per_share: float


@dataclass(slots=True)
class _PendingEntry:
    symbol: str
    direction: str
    qty: int
    stop: float
    target: float


def run_backtest(
    bars_by_date: dict[date, dict[str, list[Bar]]],
    *,
    capital_inr: float | None = None,
    orb_kwargs: dict[str, object] | None = None,
) -> BacktestResult:
    """Replay all (date, symbol) bars chronologically. Returns trades + sessions.

    `orb_kwargs` is forwarded to `ORBStrategy(**orb_kwargs)` — used by the
    parameter sweep to vary or_window_minutes / volume_multiplier /
    target_r_multiple without changing live defaults.
    """
    settings = get_settings()
    cap = capital_inr if capital_inr is not None else settings.capital_inr

    dates = sorted(bars_by_date.keys())
    result = BacktestResult(
        start_ist=dates[0].isoformat() if dates else "",
        end_ist=dates[-1].isoformat() if dates else "",
        capital_inr=cap,
    )
    for d in dates:
        sess = _replay_one_day(bars_by_date[d], d, cap, result.trades, orb_kwargs)
        result.sessions.append(sess)
    return result


def _replay_one_day(
    day_bars: dict[str, list[Bar]],
    ist_date: date,
    capital_inr: float,
    sink_trades: list[BacktestTrade],
    orb_kwargs: dict[str, object] | None = None,
) -> BacktestSession:
    settings = get_settings()
    risk_pct = settings.risk_per_trade_pct

    orb = ORBStrategy(**(orb_kwargs or {}))  # type: ignore[arg-type]
    open_positions: dict[str, _OpenPos] = {}
    pending_entries: dict[str, _PendingEntry] = {}
    stopped_out_today: set[str] = set()
    trades_today = 0
    day_pnl = 0.0
    blocked_max_trades = 0
    blocked_reentry = 0
    blocked_already_open = 0
    blocked_sizing = 0
    signals_fired = 0

    events = sorted(
        ((bar.close_time, symbol, bar) for symbol, bars in day_bars.items() for bar in bars),
        key=lambda e: (e[0], e[1]),
    )

    for _, symbol, bar in events:
        # 1) Fill any pending entry for this symbol — uses this bar's open.
        if symbol in pending_entries:
            pe = pending_entries.pop(symbol)
            side = "BUY" if pe.direction == "long" else "SELL"
            fill_price = bar.open * (1 + SLIP) if side == "BUY" else bar.open * (1 - SLIP)
            entry_charges = charges_for_leg(side=side, qty=pe.qty, price=fill_price)
            open_positions[symbol] = _OpenPos(
                symbol=symbol,
                direction=pe.direction,
                qty=pe.qty,
                entry_price=fill_price,
                entry_charges=entry_charges,
                entry_time_ist=bar.open_time.astimezone(IST).strftime("%H:%M:%S"),
                stop=pe.stop,
                target=pe.target,
                initial_risk_per_share=abs(fill_price - pe.stop),
            )

        # 2) For an open position on this symbol, check exits using this bar's
        #    range (high/low), the time-stop, and finally end-of-day forced close.
        if symbol in open_positions:
            pos = open_positions[symbol]
            exit_info = _maybe_exit(pos, bar)
            if exit_info is not None:
                exit_price, exit_reason = exit_info
                trade = _close_position(pos, bar, exit_price, exit_reason, ist_date)
                sink_trades.append(trade)
                day_pnl += trade.net_pnl
                if exit_reason == "stop_hit":
                    stopped_out_today.add(symbol)
                del open_positions[symbol]

        # 3) Feed bar to ORB. Pre-09:15 IST bars produce nothing.
        signal = orb.on_bar(bar)
        if signal is None:
            continue
        signals_fired += 1

        # 4) Gates that don't need the LLM.
        if trades_today >= MAX_TRADES_PER_DAY:
            blocked_max_trades += 1
            continue
        if symbol in open_positions:
            blocked_already_open += 1
            continue
        if symbol in stopped_out_today:
            blocked_reentry += 1
            continue

        available = capital_inr - sum(p.qty * p.entry_price for p in open_positions.values())
        sizing = size_position(
            capital_inr=capital_inr,
            available_capital_inr=available,
            risk_pct=risk_pct,
            entry=signal.breakout_price,
            stop=signal.stop,
        )
        if not sizing.accepted:
            blocked_sizing += 1
            continue

        # 5) Schedule entry on the *next* bar for this symbol.
        pending_entries[symbol] = _PendingEntry(
            symbol=symbol,
            direction=signal.direction,
            qty=sizing.qty,
            stop=signal.stop,
            target=signal.target,
        )
        trades_today += 1

    # End-of-day cleanup: any position still open exits at the last bar's close
    # as a synthetic time-stop. Shouldn't happen often since the 15:15 IST check
    # inside _maybe_exit covers normal sessions.
    for symbol, pos in list(open_positions.items()):
        last_bar = day_bars[symbol][-1] if day_bars.get(symbol) else None
        if last_bar is None:
            continue
        side = "SELL" if pos.direction == "long" else "BUY"
        fill = last_bar.close * (1 - SLIP) if side == "SELL" else last_bar.close * (1 + SLIP)
        trade = _close_position(pos, last_bar, fill, "time_stop", ist_date)
        sink_trades.append(trade)
        day_pnl += trade.net_pnl
        del open_positions[symbol]

    return BacktestSession(
        date_ist=ist_date.isoformat(),
        signals_fired=signals_fired,
        signals_blocked_by_max_trades=blocked_max_trades,
        signals_blocked_by_reentry=blocked_reentry,
        signals_blocked_by_already_open=blocked_already_open,
        signals_blocked_by_sizing=blocked_sizing,
        trades_taken=trades_today,
        net_pnl_day=round(day_pnl, 2),
    )


def _maybe_exit(pos: _OpenPos, bar: Bar) -> tuple[float, str] | None:
    """Decide whether this bar exits the position. Returns (fill_price, reason)."""
    open_ist = bar.open_time.astimezone(IST).time()
    if open_ist >= TIME_STOP_IST:
        # Time stop fires at the bar that *opens* at or after 15:15 IST.
        side_out = "SELL" if pos.direction == "long" else "BUY"
        fill = bar.open * (1 - SLIP) if side_out == "SELL" else bar.open * (1 + SLIP)
        return (fill, "time_stop")

    if pos.direction == "long":
        # Pessimistic: if both stop and target are within the bar's range,
        # assume stop hit first.
        if bar.low <= pos.stop:
            fill = pos.stop * (1 - SLIP)  # slippage against us on the way out
            return (fill, "stop_hit")
        if bar.high >= pos.target:
            fill = pos.target * (1 - SLIP)
            return (fill, "target_hit")
    else:  # short
        if bar.high >= pos.stop:
            fill = pos.stop * (1 + SLIP)
            return (fill, "stop_hit")
        if bar.low <= pos.target:
            fill = pos.target * (1 + SLIP)
            return (fill, "target_hit")
    return None


def _close_position(
    pos: _OpenPos, bar: Bar, fill_price: float, reason: str, ist_date: date
) -> BacktestTrade:
    side_out = "SELL" if pos.direction == "long" else "BUY"
    exit_charges = charges_for_leg(side=side_out, qty=pos.qty, price=fill_price)
    if pos.direction == "long":
        gross = (fill_price - pos.entry_price) * pos.qty
    else:
        gross = (pos.entry_price - fill_price) * pos.qty
    net = gross - pos.entry_charges - exit_charges
    r_mult = net / (pos.initial_risk_per_share * pos.qty) if pos.initial_risk_per_share > 0 else 0.0
    return BacktestTrade(
        date_ist=ist_date.isoformat(),
        symbol=pos.symbol,
        direction=pos.direction,
        qty=pos.qty,
        entry_time_ist=pos.entry_time_ist,
        entry_price=round(pos.entry_price, 2),
        entry_charges=round(pos.entry_charges, 2),
        exit_time_ist=bar.open_time.astimezone(IST).strftime("%H:%M:%S"),
        exit_price=round(fill_price, 2),
        exit_charges=round(exit_charges, 2),
        exit_reason=reason,
        stop=round(pos.stop, 2),
        target=round(pos.target, 2),
        gross_pnl=round(gross, 2),
        net_pnl=round(net, 2),
        r_multiple=round(r_mult, 3),
    )
