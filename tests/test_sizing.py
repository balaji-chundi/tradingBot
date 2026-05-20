from __future__ import annotations

from app.risk.sizing import size_position


def test_basic_sizing_floor() -> None:
    # capital 50k, risk 1% = ₹500, stop distance ₹20 → qty 25.
    # Notional = 25 × 600 = ₹15,000, well within 0.9×50k = 45k cap.
    res = size_position(
        capital_inr=50_000, available_capital_inr=50_000, risk_pct=1.0, entry=600.0, stop=580.0
    )
    assert res.accepted
    assert res.qty == 25  # 500 / 20 = 25.0
    assert res.notional == 25 * 600.0
    assert res.risk_amount == 500.0


def test_qty_floored_down() -> None:
    # 500 / 17 = 29.41 → floor to 29
    res = size_position(
        capital_inr=50_000, available_capital_inr=50_000, risk_pct=1.0, entry=1000.0, stop=983.0
    )
    assert res.accepted
    assert res.qty == 29


def test_rejects_when_stop_too_wide() -> None:
    # ₹50k capital, stop 600 wide → 500/600 < 1
    res = size_position(
        capital_inr=50_000, available_capital_inr=50_000, risk_pct=1.0, entry=1000.0, stop=400.0
    )
    assert not res.accepted
    assert res.rejected_reason == "qty_below_1_stop_too_wide"


def test_rejects_when_notional_exceeds_90pct_capital() -> None:
    # qty would be 25 @ 2500 = 62,500 notional but available is 50k → rejected
    res = size_position(
        capital_inr=50_000, available_capital_inr=50_000, risk_pct=1.0, entry=2500.0, stop=2480.0
    )
    # Default case: notional 62500 > 0.9 * 50000 = 45000 → rejected
    assert res.qty == 25
    assert res.rejected_reason == "notional_exceeds_90pct_available"


def test_zero_stop_distance_rejected() -> None:
    res = size_position(
        capital_inr=50_000, available_capital_inr=50_000, risk_pct=1.0, entry=2500.0, stop=2500.0
    )
    assert not res.accepted
    assert res.rejected_reason == "stop==entry"


def test_zero_entry_rejected() -> None:
    res = size_position(
        capital_inr=50_000, available_capital_inr=50_000, risk_pct=1.0, entry=0.0, stop=10.0
    )
    assert not res.accepted
    assert res.rejected_reason == "entry<=0"


def test_passes_when_notional_within_cap() -> None:
    # qty 5 @ 1000 = ₹5000 notional; well within 90% of ₹50k.
    res = size_position(
        capital_inr=50_000, available_capital_inr=50_000, risk_pct=1.0, entry=1000.0, stop=900.0
    )
    assert res.accepted
    assert res.qty == 5  # 500/100
    assert res.notional == 5000.0
