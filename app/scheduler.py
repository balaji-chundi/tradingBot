"""AsyncIOScheduler that fires Tier 1 regime checks every 15 min IST.

The brief calls for 09:30, 09:45, …, 15:00 IST. apscheduler's CronTrigger is
slightly broader than that interval-with-bounds idiom, so we fire every 15
minutes during the trading day on weekdays and filter the out-of-window
invocations inside the task (no signals depend on perfect schedule anyway —
the engine reads the latest stored verdict).

NSE holiday calendar is Phase 6; right now we just skip weekends.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import IST
from app.util.calendar import is_trading_day

log = structlog.get_logger()

REGIME_ACTIVE_FROM = (9, 30)
REGIME_ACTIVE_UNTIL = (15, 0)


class RegimeScheduler:
    def __init__(self, regime_task: Callable[[], Awaitable[Any]]) -> None:
        self._task = regime_task
        self._scheduler = AsyncIOScheduler(timezone=IST)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        trigger = CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="0,15,30,45",
            timezone=IST,
        )
        self._scheduler.add_job(
            self._guarded_task,
            trigger=trigger,
            id="regime_check",
            name="Tier 1 regime check (every 15 min IST)",
            misfire_grace_time=60,
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.start()
        self._started = True
        log.info(
            "regime_scheduler_started",
            window=f"{REGIME_ACTIVE_FROM[0]:02d}:{REGIME_ACTIVE_FROM[1]:02d}"
            f"-{REGIME_ACTIVE_UNTIL[0]:02d}:{REGIME_ACTIVE_UNTIL[1]:02d} IST",
        )

    def shutdown(self) -> None:
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False
        log.info("regime_scheduler_stopped")

    async def _guarded_task(self) -> None:
        now_ist = datetime.now(IST)
        if not is_trading_day(now_ist.date()):
            log.debug("regime_skip_holiday", date=str(now_ist.date()))
            return
        hm = (now_ist.hour, now_ist.minute)
        if hm < REGIME_ACTIVE_FROM or hm > REGIME_ACTIVE_UNTIL:
            log.debug("regime_skip_out_of_window", now_ist=now_ist.isoformat())
            return
        try:
            await self._task()
        except Exception as e:
            log.error("regime_task_failed", error=str(e))
