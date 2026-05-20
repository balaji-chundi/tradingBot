"""Async orchestrator coordinating the Phase 1 data pipeline.

Pipeline: feed thread → tick queue → consumer (persists + aggregates into bars)
→ bar queue → bar writer.

Owned by the FastAPI lifespan in [[app.main]]. Skips itself with a warning if
Angel One auth tokens or symbol-token cache aren't on disk yet — `/health` still
works in that mode, useful before `python -m app.scripts.auth` has been run.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import structlog

from app.brokers.angelone import load_tokens
from app.data.bars import BarAggregator
from app.data.feed import AngelFeed
from app.data.store import write_bar, write_ticks_batch
from app.data.types import Bar, Tick
from app.journal.db import get_session_factory
from app.strategy.universe import load_token_map

log = structlog.get_logger()

TICK_QUEUE_MAX = 10_000
BAR_QUEUE_MAX = 1_000
TICK_BATCH_SIZE = 100
TICK_FLUSH_INTERVAL_S = 1.0


class Orchestrator:
    def __init__(self) -> None:
        self.tick_q: asyncio.Queue[Tick] = asyncio.Queue(maxsize=TICK_QUEUE_MAX)
        self.bar_q: asyncio.Queue[Bar] = asyncio.Queue(maxsize=BAR_QUEUE_MAX)
        self.stop_event = asyncio.Event()
        self.feed: AngelFeed | None = None
        self.feed_thread: threading.Thread | None = None
        self.tasks: list[asyncio.Task[Any]] = []

    async def start(self) -> bool:
        """Returns True if the feed actually started, False if prerequisites missing."""
        try:
            tokens = load_tokens()
            token_map = load_token_map()
        except FileNotFoundError as e:
            log.warning("orchestrator_skip", reason=str(e))
            return False
        if tokens.expired:
            log.warning("orchestrator_skip", reason="auth tokens expired; re-run app.scripts.auth")
            return False

        loop = asyncio.get_running_loop()
        self.feed = AngelFeed(tokens, token_map, self.tick_q, loop)
        self.feed_thread = threading.Thread(
            target=self.feed.run_forever, name="angel-feed", daemon=True
        )
        self.feed_thread.start()

        self.tasks.append(asyncio.create_task(self._tick_consumer(), name="tick-consumer"))
        self.tasks.append(asyncio.create_task(self._bar_writer(), name="bar-writer"))
        log.info(
            "orchestrator_started",
            symbols=len(token_map),
            client=tokens.client_code,
        )
        return True

    async def stop(self) -> None:
        self.stop_event.set()
        if self.feed is not None:
            self.feed.stop()
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.tasks.clear()
        if self.feed_thread is not None:
            self.feed_thread.join(timeout=5)
        log.info("orchestrator_stopped")

    async def _tick_consumer(self) -> None:
        session_factory = get_session_factory()
        agg = BarAggregator()
        batch: list[Tick] = []

        async def flush() -> None:
            nonlocal batch
            if not batch:
                return
            n = await write_ticks_batch(batch, session_factory)
            log.debug("ticks_persisted", n=n)
            batch = []

        try:
            while not self.stop_event.is_set():
                try:
                    tick = await asyncio.wait_for(self.tick_q.get(), timeout=TICK_FLUSH_INTERVAL_S)
                except TimeoutError:
                    await flush()
                    continue

                batch.append(tick)
                closed = agg.ingest(tick)
                if closed is not None:
                    await self.bar_q.put(closed)
                if len(batch) >= TICK_BATCH_SIZE:
                    await flush()
        finally:
            await flush()
            for bar in agg.flush_all():
                try:
                    self.bar_q.put_nowait(bar)
                except asyncio.QueueFull:
                    pass

    async def _bar_writer(self) -> None:
        session_factory = get_session_factory()
        while not self.stop_event.is_set():
            try:
                bar = await asyncio.wait_for(self.bar_q.get(), timeout=1.0)
            except TimeoutError:
                continue
            await write_bar(bar, session_factory)
            log.info(
                "bar_closed",
                symbol=bar.symbol,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
