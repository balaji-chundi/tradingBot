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

    # Hermetic tests: blank out any credentials a real .env on the deploy box
    # might have populated. Individual tests opt back in via monkeypatch.setenv
    # (e.g. dashboard auth tests set DASHBOARD_USER + DASHBOARD_PASSWORD).
    # Empty string wins over the .env value because os.environ has higher
    # precedence in pydantic-settings.
    for var in (
        "DASHBOARD_USER",
        "DASHBOARD_PASSWORD",
        "ANGELONE_API_KEY",
        "ANGELONE_CLIENT_CODE",
        "ANGELONE_MPIN",
        "ANGELONE_TOTP_SECRET",
        "GEMINI_API_KEY",
    ):
        monkeypatch.setenv(var, "")

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
