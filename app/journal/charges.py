"""NSE intraday equity charges calculator.

Per the brief (Section 8): brokerage ₹20 flat per leg + STT 0.025% on sell-side
only + exchange transaction 0.00325% per leg + GST 18% on (brokerage + exchange)
+ SEBI ₹10 per crore (= 1e-6 of turnover).

Stamp duty (0.003% on buy-side) is intentionally *not* included here — the
brief omits it. Add later if we want stricter realism; expect ₹3–10 extra per
trade for typical ₹10k–30k positions.

Functions return positive numbers (a cost to the trader).
"""

from __future__ import annotations

BROKERAGE_PER_LEG_INR = 20.0
STT_RATE_SELL_SIDE = 0.00025  # 0.025% on sell-side turnover
EXCH_TXN_RATE_PER_LEG = 0.0000325  # 0.00325% per leg
GST_RATE = 0.18  # 18% on (brokerage + exchange txn + SEBI)
SEBI_TURNOVER_RATE = 1e-6  # ₹10 per crore = 10/10_000_000


def charges_for_leg(*, side: str, qty: int, price: float) -> float:
    """Compute total charges for one execution leg (buy or sell)."""
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    turnover = qty * price
    brokerage = BROKERAGE_PER_LEG_INR
    stt = STT_RATE_SELL_SIDE * turnover if side == "SELL" else 0.0
    exch_txn = EXCH_TXN_RATE_PER_LEG * turnover
    sebi = SEBI_TURNOVER_RATE * turnover
    gst = GST_RATE * (brokerage + exch_txn + sebi)
    return brokerage + stt + exch_txn + sebi + gst


def round_trip_charges(*, qty: int, buy_price: float, sell_price: float) -> float:
    """Total charges for a complete intraday round trip (buy + sell)."""
    return charges_for_leg(side="BUY", qty=qty, price=buy_price) + charges_for_leg(
        side="SELL", qty=qty, price=sell_price
    )
