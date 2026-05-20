from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.data.bars import BarAggregator
from app.data.types import Tick


def _tick(symbol: str, ts: datetime, ltp: float, tv: int, ltq: int = 1) -> Tick:
    return Tick(
        symbol=symbol,
        token="2885",
        ltp=ltp,
        ltq=ltq,
        total_volume=tv,
        ts=ts,
    )


def test_first_tick_opens_bar_no_emit() -> None:
    agg = BarAggregator()
    t0 = datetime(2026, 5, 20, 9, 15, 0, tzinfo=UTC)
    out = agg.ingest(_tick("RELIANCE-EQ", t0, 2500.0, 1000))
    assert out is None


def test_bar_closes_when_minute_advances() -> None:
    agg = BarAggregator()
    t0 = datetime(2026, 5, 20, 9, 15, 12, tzinfo=UTC)
    agg.ingest(_tick("RELIANCE-EQ", t0, 2500.0, 1000))
    agg.ingest(_tick("RELIANCE-EQ", t0 + timedelta(seconds=10), 2510.0, 1100))
    agg.ingest(_tick("RELIANCE-EQ", t0 + timedelta(seconds=30), 2505.0, 1150))

    # Next minute → previous bar closes
    bar = agg.ingest(_tick("RELIANCE-EQ", t0 + timedelta(minutes=1, seconds=1), 2515.0, 1200))
    assert bar is not None
    assert bar.symbol == "RELIANCE-EQ"
    assert bar.open_time == datetime(2026, 5, 20, 9, 15, 0, tzinfo=UTC)
    assert bar.close_time == datetime(2026, 5, 20, 9, 16, 0, tzinfo=UTC)
    assert bar.open == 2500.0
    assert bar.high == 2510.0
    assert bar.low == 2500.0
    assert bar.close == 2505.0
    assert bar.volume == 150  # 1150 - 1000


def test_bars_are_per_symbol() -> None:
    agg = BarAggregator()
    t0 = datetime(2026, 5, 20, 9, 15, 0, tzinfo=UTC)
    agg.ingest(_tick("RELIANCE-EQ", t0, 2500.0, 1000))
    agg.ingest(_tick("HDFCBANK-EQ", t0, 1600.0, 500))

    # Advance RELIANCE's minute but not HDFCBANK's
    bar = agg.ingest(_tick("RELIANCE-EQ", t0 + timedelta(minutes=1), 2510.0, 1100))
    assert bar is not None and bar.symbol == "RELIANCE-EQ"

    bar = agg.ingest(_tick("HDFCBANK-EQ", t0 + timedelta(seconds=45), 1605.0, 510))
    assert bar is None  # still in HDFCBANK's first minute


def test_volume_delta_is_non_negative_even_with_decreasing_tv() -> None:
    agg = BarAggregator()
    t0 = datetime(2026, 5, 20, 9, 15, 0, tzinfo=UTC)
    agg.ingest(_tick("RELIANCE-EQ", t0, 2500.0, 1000))
    # Glitch: tv drops (shouldn't happen in real feeds, but be defensive)
    agg.ingest(_tick("RELIANCE-EQ", t0 + timedelta(seconds=20), 2505.0, 999))
    bar = agg.ingest(_tick("RELIANCE-EQ", t0 + timedelta(minutes=1, seconds=1), 2510.0, 1100))
    assert bar is not None
    # last_tv pinned at max(1000, 999, 1100) = 1100 ... wait, no — the closing
    # tick (1100) is the next minute, so it doesn't update the closed bar.
    # Closed bar last_tv = max(1000, 999) = 1000, volume = 1000 - 1000 = 0
    assert bar.volume == 0


def test_flush_all_emits_in_progress_bars() -> None:
    agg = BarAggregator()
    t0 = datetime(2026, 5, 20, 9, 15, 0, tzinfo=UTC)
    agg.ingest(_tick("RELIANCE-EQ", t0, 2500.0, 1000))
    agg.ingest(_tick("HDFCBANK-EQ", t0, 1600.0, 500))
    bars = list(agg.flush_all())
    assert len(bars) == 2
    assert {b.symbol for b in bars} == {"RELIANCE-EQ", "HDFCBANK-EQ"}
    # State cleared
    assert list(agg.flush_all()) == []


def test_open_high_low_close_with_many_ticks() -> None:
    agg = BarAggregator()
    t0 = datetime(2026, 5, 20, 9, 15, 0, tzinfo=UTC)
    prices = [2500.0, 2495.0, 2510.0, 2502.0, 2508.0]
    for i, p in enumerate(prices):
        agg.ingest(_tick("RELIANCE-EQ", t0 + timedelta(seconds=i * 5), p, 1000 + i * 10))
    bar = agg.ingest(_tick("RELIANCE-EQ", t0 + timedelta(minutes=1), 2511.0, 1050))
    assert bar is not None
    assert bar.open == 2500.0
    assert bar.high == 2510.0
    assert bar.low == 2495.0
    assert bar.close == 2508.0
    assert bar.volume == 40
