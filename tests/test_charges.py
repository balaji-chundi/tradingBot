from __future__ import annotations

import pytest

from app.journal.charges import charges_for_leg, round_trip_charges


def test_buy_leg_components() -> None:
    """₹2500 × 4 = ₹10k turnover; ₹20 brokerage + STT 0 + 0.0000325 txn + 1e-6 SEBI + 18% GST."""
    c = charges_for_leg(side="BUY", qty=4, price=2500.0)
    expected = (
        20.0  # brokerage
        + 0.0  # STT (buy)
        + 0.325  # exch txn = 0.0000325 * 10000
        + 0.01  # SEBI = 1e-6 * 10000
        + 0.18 * (20.0 + 0.325 + 0.01)  # GST 18% on (brokerage + txn + sebi)
    )
    assert c == pytest.approx(expected)


def test_sell_leg_adds_stt() -> None:
    """STT 0.025% on sell side: 0.00025 * 10000 = 2.5"""
    c = charges_for_leg(side="SELL", qty=4, price=2500.0)
    expected = (
        20.0
        + 2.5  # STT
        + 0.325
        + 0.01
        + 0.18 * (20.0 + 0.325 + 0.01)
    )
    assert c == pytest.approx(expected)


def test_round_trip_at_typical_position_size() -> None:
    """Round trip on a ₹10k position with 50 bp gain — charges should eat into P&L noticeably."""
    qty = 4
    buy = 2500.0
    sell = 2512.5  # +0.5% (₹50 gross)
    c = round_trip_charges(qty=qty, buy_price=buy, sell_price=sell)
    # Charges should be roughly ₹46-50 for this turnover.
    assert 40.0 < c < 60.0


def test_invalid_side_raises() -> None:
    with pytest.raises(ValueError):
        charges_for_leg(side="LONG", qty=1, price=100.0)
