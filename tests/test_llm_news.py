from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.llm.news import Headline, filter_recent_about_symbol, titles


def _h(title: str, *, ago_minutes: int, now: datetime) -> Headline:
    return Headline(title=title, published_at=now - timedelta(minutes=ago_minutes), source="test")


def test_filter_picks_only_recent_matches() -> None:
    now = datetime(2026, 5, 20, 7, 0, tzinfo=UTC)
    h1 = _h("RELIANCE Q4 beats estimates", ago_minutes=5, now=now)
    h2 = _h("Reliance Jio launches new plan", ago_minutes=20, now=now)  # case-insensitive
    h3 = _h("HDFCBANK regulator concern", ago_minutes=10, now=now)
    h4 = _h("RELIANCE old news", ago_minutes=120, now=now)  # too old

    out = filter_recent_about_symbol(
        [h1, h2, h3, h4], symbol="RELIANCE-EQ", minutes=30, now_utc=now
    )
    assert out == [h1.title, h2.title]


def test_filter_handles_undated_headlines() -> None:
    h = Headline(title="RELIANCE breaking news", published_at=None, source="test")
    now = datetime(2026, 5, 20, 7, 0, tzinfo=UTC)
    out = filter_recent_about_symbol([h], symbol="RELIANCE-EQ", minutes=30, now_utc=now)
    # Without a published_at, we don't know the age — be inclusive (don't filter out)
    assert out == [h.title]


def test_titles_helper() -> None:
    now = datetime(2026, 5, 20, 7, 0, tzinfo=UTC)
    items = [_h("A", ago_minutes=1, now=now), _h("B", ago_minutes=2, now=now)]
    assert titles(items) == ["A", "B"]
