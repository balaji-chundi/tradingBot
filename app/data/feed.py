"""Angel One SmartWebSocketV2 consumer.

The SDK's WebSocket client is synchronous and blocks the calling thread inside
its `.connect()` method. We run it in a dedicated daemon thread and bridge each
tick into an `asyncio.Queue` via `loop.call_soon_threadsafe`.

Prices arrive in paise — we divide by 100 before constructing the [[tick-type]].
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from app.brokers.angelone import AuthTokens
from app.config import get_settings
from app.data.types import Tick
from app.strategy.universe import EXCHANGE_NSE_CM

log = structlog.get_logger()

QUOTE_MODE = 2
CORRELATION_ID = "trading_app_feed"
MAX_BACKOFF_S = 60.0


class AngelFeed:
    """Thread-backed WebSocket reader that emits Tick objects to an asyncio queue.

    Lifecycle:
      feed = AngelFeed(tokens, token_map, queue, loop)
      threading.Thread(target=feed.run_forever, daemon=True).start()
      ...
      feed.stop()  # idempotent
    """

    def __init__(
        self,
        tokens: AuthTokens,
        token_map: dict[str, str],
        out_queue: asyncio.Queue[Tick],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._tokens = tokens
        self._token_to_symbol = {v: k for k, v in token_map.items()}
        self._out_queue = out_queue
        self._loop = loop
        self._stop = threading.Event()
        self._sws: Any = None
        self._last_message_at = 0.0

    @property
    def last_message_age_s(self) -> float:
        """Seconds since the last tick arrived. `inf` if we haven't seen any."""
        if self._last_message_at == 0:
            return float("inf")
        return time.monotonic() - self._last_message_at

    def run_forever(self) -> None:
        """Blocking. Connects, runs until `stop()` is called, reconnects on drop."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect_once()
                backoff = 1.0
            except Exception as e:
                log.error("feed_connect_error", error=str(e))
            if self._stop.is_set():
                break
            log.warning("feed_reconnect_pending", retry_in_s=backoff)
            self._stop.wait(timeout=backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    def stop(self) -> None:
        self._stop.set()
        sws = self._sws
        if sws is not None:
            for closer in ("close_connection", "close", "disconnect"):
                fn = getattr(sws, closer, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
                    break

    def _connect_once(self) -> None:
        from app.brokers.angelone import _stub_legacy_smartapi

        _stub_legacy_smartapi()
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        settings = get_settings()
        sws = SmartWebSocketV2(
            auth_token=self._tokens.jwt_token,
            api_key=settings.angelone_api_key,
            client_code=self._tokens.client_code,
            feed_token=self._tokens.feed_token,
        )
        sws.on_open = self._on_open
        sws.on_data = self._on_data
        sws.on_error = self._on_error
        sws.on_close = self._on_close
        self._sws = sws
        sws.connect()

    def _on_open(self, wsapp: Any) -> None:
        tokens = list(self._token_to_symbol.keys())
        log.info("feed_open", tokens=len(tokens))
        assert self._sws is not None
        self._sws.subscribe(
            CORRELATION_ID,
            QUOTE_MODE,
            [{"exchangeType": EXCHANGE_NSE_CM, "tokens": tokens}],
        )

    def _on_data(self, wsapp: Any, message: dict[str, Any]) -> None:
        tick = self._to_tick(message)
        if tick is None:
            return
        self._last_message_at = time.monotonic()
        self._loop.call_soon_threadsafe(self._enqueue, tick)

    def _enqueue(self, tick: Tick) -> None:
        try:
            self._out_queue.put_nowait(tick)
        except asyncio.QueueFull:
            log.warning("feed_queue_full_drop", symbol=tick.symbol)

    def _on_error(self, wsapp: Any, error: Any, *_: Any) -> None:
        log.error("feed_ws_error", error=str(error))

    def _on_close(self, wsapp: Any, *_: Any) -> None:
        # The SDK has historically invoked on_close with either 1 or 3 positional
        # args depending on which underlying ws library is in use; accept any.
        log.info("feed_ws_close")

    def _to_tick(self, message: dict[str, Any]) -> Tick | None:
        token = str(message.get("token") or "")
        symbol = self._token_to_symbol.get(token)
        if not symbol:
            return None
        ltp_paise = message.get("last_traded_price")
        if ltp_paise is None:
            return None
        try:
            ltp_inr = float(ltp_paise) / 100.0
            ltq = int(message.get("last_traded_quantity") or 0)
            total_volume = int(message.get("volume_trade_for_the_day") or 0)
            exch_ts = message.get("exchange_timestamp")
            exch_ts_ms = int(exch_ts) if exch_ts is not None else None
        except (TypeError, ValueError) as e:
            log.warning("feed_tick_parse_error", error=str(e), token=token)
            return None
        return Tick(
            symbol=symbol,
            token=token,
            ltp=ltp_inr,
            ltq=ltq,
            total_volume=total_volume,
            ts=datetime.now(UTC),
            exchange_ts_ms=exch_ts_ms,
            raw=message,
        )
