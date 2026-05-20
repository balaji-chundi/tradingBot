from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import IST
from app.data.types import Bar
from app.strategy.orb import ORBStrategy

DAY = date(2026, 5, 20)


def b(
    ist_hh: int,
    ist_mm: int,
    *,
    o: float,
    h: float,
    low: float,
    c: float,
    v: int,
    symbol: str = "RELIANCE-EQ",
    day: date = DAY,
) -> Bar:
    open_ist = datetime(day.year, day.month, day.day, ist_hh, ist_mm, tzinfo=IST)
    open_utc = open_ist.astimezone(UTC)
    return Bar(
        symbol=symbol,
        open_time=open_utc,
        close_time=open_utc + timedelta(minutes=1),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


def _build_or_window(
    strat: ORBStrategy, *, or_high: float = 2510.0, or_low: float = 2490.0
) -> None:
    """Feed 15 OR bars (09:15..09:29) establishing or_high/or_low and warming volume window."""
    for i in range(15):
        # The first bar pins highs/lows; later bars stay strictly inside.
        if i == 0:
            high = or_high
            low = or_low
        else:
            high = or_high - 1.0
            low = or_low + 1.0
        out = strat.on_bar(
            b(
                9,
                15 + i,
                o=(or_high + or_low) / 2,
                h=high,
                low=low,
                c=(or_high + or_low) / 2,
                v=1000,
            )
        )
        assert out is None  # OR-window bars never emit


def test_or_window_bars_dont_emit_signals() -> None:
    strat = ORBStrategy()
    _build_or_window(strat)
    # Internal state via probing the next post-OR bar that doesn't break out
    out = strat.on_bar(b(9, 30, o=2500, h=2505, low=2495, c=2500, v=1000))
    assert out is None


def test_clean_long_breakout_emits_signal_with_correct_stop_and_target() -> None:
    strat = ORBStrategy()
    _build_or_window(strat, or_high=2510.0, or_low=2490.0)
    sig = strat.on_bar(b(9, 30, o=2510, h=2520, low=2509, c=2518.0, v=2000))
    assert sig is not None
    assert sig.symbol == "RELIANCE-EQ"
    assert sig.direction == "long"
    assert sig.breakout_price == 2518.0
    assert sig.or_high == 2510.0
    assert sig.or_low == 2490.0
    assert sig.stop == 2490.0  # other side of the OR
    # target = entry + 1.5 * (entry - stop) = 2518 + 1.5*(2518 - 2490) = 2518 + 42 = 2560
    assert sig.target == pytest.approx(2560.0)
    assert sig.bar_volume == 2000
    assert sig.avg_prior_5bar_volume == pytest.approx(1000.0)
    assert sig.volume_ratio == pytest.approx(2.0)


def test_clean_short_breakout_emits_signal_with_correct_stop_and_target() -> None:
    strat = ORBStrategy()
    _build_or_window(strat, or_high=2510.0, or_low=2490.0)
    sig = strat.on_bar(b(9, 30, o=2490, h=2491, low=2480, c=2482.0, v=2000))
    assert sig is not None
    assert sig.direction == "short"
    assert sig.stop == 2510.0  # OR high
    # target = entry - 1.5 * (stop - entry) = 2482 - 1.5*28 = 2482 - 42 = 2440
    assert sig.target == pytest.approx(2440.0)


def test_fake_breakout_no_volume_filter_does_not_signal() -> None:
    strat = ORBStrategy()
    _build_or_window(strat, or_high=2510.0, or_low=2490.0)
    # Close above OR high, but volume only 1.4× avg (need > 1.5×)
    out = strat.on_bar(b(9, 30, o=2510, h=2520, low=2509, c=2518.0, v=1400))
    assert out is None


def test_volume_exactly_at_multiplier_does_not_signal() -> None:
    strat = ORBStrategy()
    _build_or_window(strat, or_high=2510.0, or_low=2490.0)
    # Volume ratio exactly 1.5 → strict `>` so we don't fire
    out = strat.on_bar(b(9, 30, o=2510, h=2520, low=2509, c=2518.0, v=1500))
    assert out is None


def test_no_breakout_close_inside_range() -> None:
    strat = ORBStrategy()
    _build_or_window(strat, or_high=2510.0, or_low=2490.0)
    out = strat.on_bar(b(9, 30, o=2500, h=2509, low=2491, c=2505.0, v=3000))
    assert out is None


def test_close_equal_to_or_high_does_not_signal() -> None:
    strat = ORBStrategy()
    _build_or_window(strat, or_high=2510.0, or_low=2490.0)
    # Strict `>` — close at OR-high exactly is not a breakout
    out = strat.on_bar(b(9, 30, o=2509, h=2510, low=2508, c=2510.0, v=2000))
    assert out is None


def test_only_one_signal_per_symbol_per_day() -> None:
    strat = ORBStrategy()
    _build_or_window(strat, or_high=2510.0, or_low=2490.0)
    s1 = strat.on_bar(b(9, 30, o=2510, h=2520, low=2509, c=2518.0, v=2000))
    assert s1 is not None
    # Another breakout in the same direction
    s2 = strat.on_bar(b(9, 31, o=2518, h=2525, low=2517, c=2523.0, v=3000))
    assert s2 is None
    # Or even the opposite direction (would have been a short)
    s3 = strat.on_bar(b(9, 32, o=2520, h=2521, low=2480, c=2482.0, v=3000))
    assert s3 is None


def test_both_sides_breakout_uses_close_direction() -> None:
    strat = ORBStrategy()
    _build_or_window(strat, or_high=2510.0, or_low=2490.0)
    # high > OR-high AND low < OR-low — but close is below OR-low → short
    sig = strat.on_bar(b(9, 30, o=2500, h=2515, low=2485, c=2486.0, v=2000))
    assert sig is not None
    assert sig.direction == "short"


def test_symbol_with_no_or_bars_skips_day() -> None:
    strat = ORBStrategy()
    # Symbol "ILLIQUID-EQ" had no trades during the OR window. First bar arrives at 09:45.
    out = strat.on_bar(b(9, 45, o=100, h=101, low=99, c=100.5, v=10_000, symbol="ILLIQUID-EQ"))
    assert out is None


def test_signals_per_symbol_are_independent() -> None:
    strat = ORBStrategy()
    _build_or_window(strat, or_high=2510.0, or_low=2490.0)
    # A second symbol with a different OR
    for i in range(15):
        strat.on_bar(b(9, 15 + i, o=1600, h=1605, low=1595, c=1600, v=500, symbol="HDFCBANK-EQ"))
    s_rel = strat.on_bar(b(9, 30, o=2510, h=2520, low=2509, c=2518.0, v=2000))
    s_hdfc = strat.on_bar(
        b(9, 30, o=1600, h=1610, low=1599, c=1609.0, v=2000, symbol="HDFCBANK-EQ")
    )
    assert s_rel is not None and s_rel.symbol == "RELIANCE-EQ"
    assert s_hdfc is not None and s_hdfc.symbol == "HDFCBANK-EQ"
    assert s_hdfc.or_high == 1605.0
    assert s_hdfc.or_low == 1595.0


def test_new_day_resets_state() -> None:
    strat = ORBStrategy()
    _build_or_window(strat, or_high=2510.0, or_low=2490.0)
    sig1 = strat.on_bar(b(9, 30, o=2510, h=2520, low=2509, c=2518.0, v=2000))
    assert sig1 is not None

    next_day = date(2026, 5, 21)
    # Build a different OR on the next day
    for i in range(15):
        strat.on_bar(b(9, 15 + i, o=2600, h=2605, low=2595, c=2600, v=1000, day=next_day))
    sig2 = strat.on_bar(b(9, 30, o=2600, h=2615, low=2599, c=2612.0, v=2000, day=next_day))
    assert sig2 is not None
    assert sig2.or_high == 2605.0
    assert sig2.or_low == 2595.0


def test_pre_915_bars_ignored() -> None:
    strat = ORBStrategy()
    out = strat.on_bar(b(9, 14, o=2500, h=2510, low=2490, c=2505, v=500))
    assert out is None  # pre-OR auction; not in the OR window, not post-OR


def test_first_post_or_bars_skipped_until_volume_window_filled() -> None:
    """If the OR window only accumulated 2 bars (subscription started late),
    post-OR breakouts can't fire until 5 prior volumes are in the rolling window."""
    strat = ORBStrategy()
    strat.on_bar(b(9, 27, o=2500, h=2510, low=2490, c=2500, v=900))
    strat.on_bar(b(9, 28, o=2500, h=2511, low=2489, c=2500, v=1100))
    # Now at 09:30, recent_volumes has 2 entries — below lookback (5), no signal
    out = strat.on_bar(b(9, 30, o=2511, h=2520, low=2510, c=2520, v=10_000))
    assert out is None
