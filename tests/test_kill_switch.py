"""Kill-switch end-to-end:

- engine.on_signal blocks new entries with reason="kill_switch"
- engine.on_tick force-exits any open position via market order regardless
  of stop/target/time-stop
- kill.py CLI sends SIGUSR1 and returns the right exit codes
"""

from __future__ import annotations

import os
import signal as sig
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.brokers.paper import PaperBroker
from app.config import IST
from app.data.types import Signal, Tick
from app.execution.engine import ExecutionEngine
from app.journal.db import get_session_factory, init_db
from app.journal.models import Order, Position


def _ist(h: int, mi: int, s: int = 0) -> datetime:
    return datetime(2026, 5, 21, h, mi, s, tzinfo=IST).astimezone(UTC)


def _sig() -> Signal:
    return Signal(
        symbol="HDFCBANK-EQ",
        direction="long",
        breakout_close_time=_ist(9, 31),
        breakout_price=700.0,
        or_high=700.0,
        or_low=680.0,
        stop=680.0,
        target=730.0,
        bar_volume=10_000,
        avg_prior_5bar_volume=5_000,
        volume_ratio=2.0,
    )


def _tick(ltp: float, ts: datetime | None = None) -> Tick:
    return Tick(
        symbol="HDFCBANK-EQ",
        token="1333",
        ltp=ltp,
        ltq=10,
        total_volume=10_000,
        ts=ts or _ist(10, 0),
    )


@pytest.mark.asyncio
async def test_kill_blocks_new_signals() -> None:
    await init_db()
    sf = get_session_factory()
    killed = {"v": True}
    broker = PaperBroker(sf)
    engine = ExecutionEngine(broker, sf, is_killed=lambda: killed["v"])
    result = await engine.on_signal(_sig())
    assert result is None
    # no orders, no signals persisted
    async with sf() as session:
        orders = (await session.execute(select(Order))).scalars().all()
    assert orders == []


@pytest.mark.asyncio
async def test_kill_force_exits_open_position() -> None:
    await init_db()
    sf = get_session_factory()
    killed = {"v": False}
    broker = PaperBroker(sf, slippage_bps=5.0)
    engine = ExecutionEngine(broker, sf, is_killed=lambda: killed["v"])

    # Open a position normally (kill switch off)
    await engine.on_signal(_sig())
    await engine.on_tick(_tick(700.0, _ist(10, 0)))  # entry fills
    async with sf() as session:
        pos = (await session.execute(select(Position))).scalar_one()
    assert pos.closed_at is None
    assert len(engine._open) == 1  # noqa: SLF001

    # Flip the kill switch BEFORE any stop/target hit; tick should force-exit.
    killed["v"] = True
    # Mid-day price, well between stop (680) and target (730) — no normal exit.
    await engine.on_tick(_tick(705.0, _ist(10, 5)))  # kill_switch exit order placed
    await engine.on_tick(_tick(705.0, _ist(10, 5, 1)))  # exit fills

    async with sf() as session:
        pos = (await session.execute(select(Position))).scalar_one()
        orders = (await session.execute(select(Order))).scalars().all()
    assert pos.closed_at is not None
    assert any((o.payload or {}).get("role") == "kill_switch" for o in orders), (
        "expected an exit order with role=kill_switch"
    )


def test_kill_cli_missing_pidfile_returns_2(tmp_path: Path) -> None:
    missing = tmp_path / "absent.pid"
    rc = subprocess.run(
        [sys.executable, "-m", "app.kill", "--pid-file", str(missing)],
        capture_output=True,
        text=True,
        check=False,
    ).returncode
    assert rc == 2


def test_kill_cli_dead_pid_returns_3(tmp_path: Path) -> None:
    # Spawn a short-lived subprocess and record its PID after it dies.
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    pidfile = tmp_path / "dead.pid"
    pidfile.write_text(str(p.pid))
    # Give the OS a moment to fully reap.
    time.sleep(0.1)
    rc = subprocess.run(
        [sys.executable, "-m", "app.kill", "--pid-file", str(pidfile)],
        capture_output=True,
        text=True,
        check=False,
    ).returncode
    assert rc == 3


def test_kill_cli_sends_sigusr1_to_self(tmp_path: Path) -> None:
    """Spawn a Python child that installs a SIGUSR1 handler and waits, then
    invoke `python -m app.kill --pid <child_pid>` and assert the child
    exited with the expected code from its handler."""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time;"
            "signal.signal(signal.SIGUSR1, lambda *_: sys.exit(42));"
            "time.sleep(5)",
        ]
    )
    try:
        # Give the child time to install its handler.
        time.sleep(0.5)
        kill_rc = subprocess.run(
            [sys.executable, "-m", "app.kill", "--pid", str(child.pid)],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        assert kill_rc == 0
        child.wait(timeout=2)
        assert child.returncode == 42
    finally:
        if child.poll() is None:
            os.kill(child.pid, sig.SIGTERM)
            child.wait(timeout=1)
