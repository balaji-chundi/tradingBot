"""Position sizing from per-trade risk percentage.

Formula (Section 5 of the brief):
    risk_per_trade = risk_pct * capital
    stop_distance  = abs(entry - stop)
    qty            = floor(risk_per_trade / stop_distance)

Rejects:
    qty < 1  (stop too wide for the risk budget)
    qty * entry > 0.9 * available_capital  (insufficient cash; no MIS leverage in v1)
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SizingResult:
    qty: int
    notional: float
    risk_amount: float
    rejected_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.rejected_reason is None and self.qty >= 1


def size_position(
    *,
    capital_inr: float,
    available_capital_inr: float,
    risk_pct: float,
    entry: float,
    stop: float,
) -> SizingResult:
    if entry <= 0:
        return SizingResult(qty=0, notional=0.0, risk_amount=0.0, rejected_reason="entry<=0")
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return SizingResult(qty=0, notional=0.0, risk_amount=0.0, rejected_reason="stop==entry")

    risk_amount = (risk_pct / 100.0) * capital_inr
    raw_qty = risk_amount / stop_distance
    qty = int(math.floor(raw_qty))

    if qty < 1:
        return SizingResult(
            qty=0,
            notional=0.0,
            risk_amount=risk_amount,
            rejected_reason="qty_below_1_stop_too_wide",
        )

    notional = qty * entry
    if notional > 0.9 * available_capital_inr:
        return SizingResult(
            qty=qty,
            notional=notional,
            risk_amount=risk_amount,
            rejected_reason="notional_exceeds_90pct_available",
        )

    return SizingResult(qty=qty, notional=notional, risk_amount=risk_amount)
