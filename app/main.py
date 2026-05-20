from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.config import get_settings
from app.dashboard.routes import router as dashboard_router
from app.journal.db import dispose_engine, init_db
from app.orchestrator import Orchestrator


def configure_logging() -> None:
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", level=level, stream=sys.stdout)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log = structlog.get_logger()
    settings = get_settings()
    log.info("startup", broker_mode=settings.broker_mode, db=str(settings.db_path))
    await init_db()
    orch = Orchestrator()
    started = await orch.start()
    try:
        yield
    finally:
        if started:
            await orch.stop()
        await dispose_engine()
        log.info("shutdown")


app = FastAPI(title="trading-app", version="0.1.0", lifespan=lifespan)
app.include_router(dashboard_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
