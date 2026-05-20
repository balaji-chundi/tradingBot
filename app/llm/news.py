"""Lightweight RSS headline fetcher.

Fail-open: any error returns an empty list — the regime call still happens
without news context. Wider news aggregation (multiple feeds, dedup, sentiment)
can come later; this is enough to seed the prompt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

log = structlog.get_logger()

# A single feed for v1. Moneycontrol's markets feed is concise and updates
# during trading hours.
DEFAULT_RSS_URL = "https://www.moneycontrol.com/rss/marketreports.xml"
DEFAULT_FETCH_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class Headline:
    title: str
    published_at: datetime | None
    source: str


async def fetch_headlines(
    url: str = DEFAULT_RSS_URL,
    *,
    limit: int = 10,
    timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
) -> list[Headline]:
    try:
        return await asyncio.wait_for(_fetch(url, limit), timeout=timeout_s)
    except (TimeoutError, Exception) as e:  # broad on purpose — fail-open
        log.warning("news_fetch_failed", url=url, error=str(e))
        return []


async def _fetch(url: str, limit: int) -> list[Headline]:
    # feedparser is sync; offload to a thread so we don't block the loop.
    import feedparser

    parsed = await asyncio.to_thread(feedparser.parse, url)
    headlines: list[Headline] = []
    for entry in (getattr(parsed, "entries", None) or [])[:limit]:
        title = getattr(entry, "title", None)
        if not title:
            continue
        published: datetime | None = None
        struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if struct is not None:
            try:
                published = datetime(
                    struct[0],
                    struct[1],
                    struct[2],
                    struct[3],
                    struct[4],
                    struct[5],
                    tzinfo=UTC,
                )
            except (TypeError, ValueError):
                published = None
        headlines.append(
            Headline(
                title=str(title).strip(),
                published_at=published,
                source=url,
            )
        )
    return headlines


def filter_recent_about_symbol(
    headlines: list[Headline],
    *,
    symbol: str,
    minutes: int = 30,
    now_utc: datetime | None = None,
) -> list[str]:
    """Return only headlines from the last N minutes mentioning the symbol's company name."""
    now = now_utc or datetime.now(UTC)
    cutoff_ts = now.timestamp() - minutes * 60
    # Strip "-EQ" suffix for substring matching
    needle = symbol.replace("-EQ", "").lower()
    out: list[str] = []
    for h in headlines:
        if h.published_at is not None and h.published_at.timestamp() < cutoff_ts:
            continue
        if needle in h.title.lower():
            out.append(h.title)
    return out


def titles(headlines: list[Headline]) -> list[str]:
    return [h.title for h in headlines]
