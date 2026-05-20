from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _isolated_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[None]:
    """Per-test temp DB and log dir. Dispose the engine on teardown so the
    aiosqlite worker thread doesn't outlive pytest-asyncio's per-test event loop.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))

    from app.journal import db as db_module

    db_module._engine = None
    db_module._session_factory = None
    try:
        yield
    finally:
        if db_module._engine is not None:
            await db_module._engine.dispose()
        db_module._engine = None
        db_module._session_factory = None
