from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.brokers.angelone import AuthTokens
from app.data.feed import AngelFeed
from app.data.types import Tick


def _make_feed(loop: asyncio.AbstractEventLoop) -> AngelFeed:
    tokens = AuthTokens(
        jwt_token="jwt",
        refresh_token="rt",
        feed_token="ft",
        client_code="B12345",
        issued_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    token_map = {
        "RELIANCE-EQ": "2885",
        "HDFCBANK-EQ": "1333",
    }
    queue: asyncio.Queue[Tick] = asyncio.Queue()
    return AngelFeed(tokens, token_map, queue, loop)


@pytest.mark.asyncio
async def test_to_tick_parses_quote_message() -> None:
    loop = asyncio.get_running_loop()
    feed = _make_feed(loop)

    msg = {
        "subscription_mode": 2,
        "exchange_type": 1,
        "token": "2885",
        "last_traded_price": 250125,  # paise → 2501.25 INR
        "last_traded_quantity": 10,
        "volume_trade_for_the_day": 1_234_567,
        "exchange_timestamp": 1716172500000,
    }
    tick = feed._to_tick(msg)  # noqa: SLF001
    assert tick is not None
    assert tick.symbol == "RELIANCE-EQ"
    assert tick.token == "2885"
    assert tick.ltp == pytest.approx(2501.25)
    assert tick.ltq == 10
    assert tick.total_volume == 1_234_567
    assert tick.exchange_ts_ms == 1716172500000
    assert tick.ts.tzinfo is not None
    assert tick.raw is msg


@pytest.mark.asyncio
async def test_to_tick_returns_none_for_unknown_token() -> None:
    loop = asyncio.get_running_loop()
    feed = _make_feed(loop)
    assert feed._to_tick({"token": "9999", "last_traded_price": 10000}) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_to_tick_returns_none_when_ltp_missing() -> None:
    loop = asyncio.get_running_loop()
    feed = _make_feed(loop)
    assert feed._to_tick({"token": "2885"}) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_on_data_enqueues_tick_to_async_queue() -> None:
    loop = asyncio.get_running_loop()
    feed = _make_feed(loop)
    # Simulate the SDK thread invoking on_data
    msg = {"token": "1333", "last_traded_price": 160050, "last_traded_quantity": 5}
    feed._on_data(MagicMock(), msg)  # noqa: SLF001
    # Give call_soon_threadsafe a chance to run
    await asyncio.sleep(0)
    tick = feed._out_queue.get_nowait()  # noqa: SLF001
    assert tick.symbol == "HDFCBANK-EQ"
    assert tick.ltp == pytest.approx(1600.50)
