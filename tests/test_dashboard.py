"""Dashboard route tests.

Boots the FastAPI app via TestClient (which runs the lifespan and the
auth-tokens-missing-skip path). Each route should return 200; partials should
render the expected key strings when seeded.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import IST
from app.journal import models as m
from app.journal.db import get_session_factory, init_db
from app.main import app


@pytest.fixture(name="client")
def _client_fixture() -> Iterator[TestClient]:
    # Use TestClient as a context manager so the lifespan runs (init_db, etc.)
    with TestClient(app) as c:
        yield c


def test_index_renders(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "trading-app" in body
    # HTMX inclusion
    assert "htmx.org" in body
    # All major sections wired
    for path in ("/partials/pnl", "/partials/positions", "/partials/signals", "/partials/regime"):
        assert path in body


def test_partial_pnl_with_empty_state(client: TestClient) -> None:
    r = client.get("/partials/pnl")
    assert r.status_code == 200
    body = r.text
    assert "Capital" in body
    assert "₹50,000" in body  # default capital
    assert "0 / 2" in body  # trades_today / max
    assert "0 / 2" in body  # open positions / max


def test_partial_positions_empty(client: TestClient) -> None:
    r = client.get("/partials/positions")
    assert r.status_code == 200
    assert "no open positions" in r.text


def test_partial_signals_empty(client: TestClient) -> None:
    r = client.get("/partials/signals")
    assert r.status_code == 200
    assert "no signals today" in r.text


def test_partial_regime_empty(client: TestClient) -> None:
    r = client.get("/partials/regime")
    assert r.status_code == 200
    assert "no regime verdict yet" in r.text


def test_partial_fills_empty(client: TestClient) -> None:
    r = client.get("/partials/fills")
    assert r.status_code == 200
    assert "Avg slippage" in r.text
    assert "no fills yet today" in r.text


def test_partial_risk_blocks_empty(client: TestClient) -> None:
    r = client.get("/partials/risk-blocks")
    assert r.status_code == 200
    assert "no risk blocks today" in r.text


def test_partial_ticker_lists_universe_even_with_no_ticks(client: TestClient) -> None:
    r = client.get("/partials/ticker")
    assert r.status_code == 200
    # All 5 universe symbols appear regardless of whether ticks are in the DB
    for s in ("RELIANCE-EQ", "HDFCBANK-EQ", "ICICIBANK-EQ", "INFY-EQ", "TCS-EQ"):
        assert s in r.text


@pytest.mark.asyncio
async def test_partial_positions_shows_seeded_row() -> None:
    await init_db()
    sf = get_session_factory()
    async with sf() as session:
        session.add(
            m.Position(
                symbol="HDFCBANK-EQ",
                qty=25,  # long
                avg_entry=700.50,
                opened_at=datetime.now(UTC),
                realised_pnl=0.0,
            )
        )
        await session.commit()

    with TestClient(app) as client:
        r = client.get("/partials/positions")
    assert r.status_code == 200
    assert "HDFCBANK-EQ" in r.text
    assert "long" in r.text
    assert "25" in r.text
    assert "700.5" in r.text


@pytest.mark.asyncio
async def test_partial_regime_shows_seeded_verdict() -> None:
    await init_db()
    sf = get_session_factory()
    async with sf() as session:
        session.add(
            m.RegimeVerdict(
                ts=datetime.now(UTC),
                regime="risk_off",
                confidence=0.82,
                key_drivers=["VIX spike", "negative breadth"],
                watch_symbols=[],
                avoid_symbols=["RELIANCE-EQ"],
                rationale="elevated tail-risk, avoid fresh entries",
            )
        )
        await session.commit()

    with TestClient(app) as client:
        r = client.get("/partials/regime")
    body = r.text
    assert "risk_off" in body
    assert "0.82" in body
    assert "VIX spike" in body
    assert "elevated tail-risk" in body


@pytest.mark.asyncio
async def test_partial_signals_shows_pretrade_decision() -> None:
    await init_db()
    sf = get_session_factory()
    async with sf() as session:
        session.add(
            m.Signal(
                symbol="HDFCBANK-EQ",
                direction="long",
                breakout_price=700.0,
                or_high=700.0,
                or_low=680.0,
                qty=12,
                stop=680.0,
                target=730.0,
                status="SUBMITTED",
                pretrade_decision="mixed signals; reduced size",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    with TestClient(app) as client:
        r = client.get("/partials/signals")
    body = r.text
    assert "HDFCBANK-EQ" in body
    assert "mixed signals" in body
    assert "SUBMITTED" in body


@pytest.mark.asyncio
async def test_partial_risk_blocks_shows_row_today_not_yesterday() -> None:
    await init_db()
    sf = get_session_factory()
    yesterday_ist = datetime.now(IST) - timedelta(days=1)
    async with sf() as session:
        session.add(
            m.RiskBlock(
                ts=datetime.now(UTC),
                reason="regime_risk_off",
                payload={"symbol": "TCS-EQ"},
            )
        )
        session.add(
            m.RiskBlock(
                ts=yesterday_ist.astimezone(UTC),
                reason="daily_loss_limit_hit",
                payload={"symbol": "INFY-EQ"},
            )
        )
        await session.commit()
    with TestClient(app) as client:
        r = client.get("/partials/risk-blocks")
    body = r.text
    assert "regime_risk_off" in body
    assert "TCS-EQ" in body
    assert "daily_loss_limit_hit" not in body  # yesterday's row filtered out
