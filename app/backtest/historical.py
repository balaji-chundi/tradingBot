"""Angel One historical-candle fetcher with on-disk cache.

The SmartAPI `/historical/v1/getCandleData` endpoint returns OHLC bars for a
(symbol, interval, date-range) tuple. Calling it requires an authenticated
SmartConnect (in-process `generateSession`); setting attributes manually
doesn't work because the server tracks session state by login.

Historical data for a past date is immutable, so we cache one JSON file per
(symbol, ist_date) under data/historical/. Re-running the backtest is then
free (no API calls).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyotp
import structlog

from app.brokers.angelone import _stub_legacy_smartapi
from app.config import IST, PROJECT_ROOT, get_settings
from app.data.types import Bar
from app.strategy.universe import HARDCODED_NSE_TOKENS, NIFTY_5_UNIVERSE
from app.util.calendar import is_trading_day

log = structlog.get_logger()

HISTORICAL_DIR = PROJECT_ROOT / "data" / "historical"
# Angel One's historical-candle endpoint throttles aggressively. Conservative
# pacing + retry-on-throttle keeps the fetch reliable. Observed empirically:
# bursts of ~10-15 calls trigger the limiter; recovery window is ~30-120s.
RATE_LIMIT_SLEEP_S = 3.0
RATE_LIMIT_BACKOFF_S = (60.0, 120.0, 300.0)


@dataclass(slots=True)
class _SessionAPI:
    """Wrapper so callers don't need to know about smartapi-python internals."""

    api: Any
    client_code: str


def authed_smart_connect() -> _SessionAPI:
    """Spin up a SmartConnect with a real generateSession.

    Burns one Angel One login. Subsequent live-orchestrator activity may have
    its in-memory access_token invalidated by Angel One when this new session
    is created — the existing WebSocket connection stays alive on its TCP
    socket, but any reconnect attempt would fail until the orchestrator
    re-authenticates. Acceptable during backtest runs.
    """
    _stub_legacy_smartapi()
    from SmartApi import SmartConnect

    settings = get_settings()
    if not (
        settings.angelone_api_key
        and settings.angelone_client_code
        and settings.angelone_mpin
        and settings.angelone_totp_secret
    ):
        raise RuntimeError("Angel One credentials missing in .env")

    api = SmartConnect(api_key=settings.angelone_api_key)
    totp = pyotp.TOTP(settings.angelone_totp_secret).now()
    resp = api.generateSession(settings.angelone_client_code, settings.angelone_mpin, totp)
    if not resp.get("status"):
        raise RuntimeError(f"login failed: {resp.get('message')} ({resp.get('errorcode')})")
    log.info("backtest_login_ok", client=settings.angelone_client_code)
    return _SessionAPI(api=api, client_code=settings.angelone_client_code)


def cache_path(symbol: str, ist_date: date) -> Path:
    HISTORICAL_DIR.mkdir(parents=True, exist_ok=True)
    return HISTORICAL_DIR / f"{symbol}__{ist_date.isoformat()}.json"


def load_cached_raw(symbol: str, ist_date: date) -> list[list[Any]] | None:
    p = cache_path(symbol, ist_date)
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    rows = raw.get("data")
    return rows if isinstance(rows, list) else None


def save_cached_raw(symbol: str, ist_date: date, payload: dict[str, Any]) -> None:
    cache_path(symbol, ist_date).write_text(json.dumps(payload, indent=2))


def fetch_day_raw(
    session: _SessionAPI,
    symbol: str,
    symbol_token: str,
    ist_date: date,
) -> list[list[Any]]:
    """Fetch one trading day's 1-min bars for one symbol. Cache-aware.

    On rate-limit errors ("Access denied because of exceeding access rate"),
    backs off and retries up to len(RATE_LIMIT_BACKOFF_S) times.
    """
    cached = load_cached_raw(symbol, ist_date)
    if cached is not None:
        return cached

    fromdate = f"{ist_date.isoformat()} 09:15"
    todate = f"{ist_date.isoformat()} 15:30"
    params = {
        "exchange": "NSE",
        "symboltoken": symbol_token,
        "interval": "ONE_MINUTE",
        "fromdate": fromdate,
        "todate": todate,
    }

    last_err: str | None = None
    for attempt, backoff in enumerate([0.0, *RATE_LIMIT_BACKOFF_S]):
        if backoff:
            log.warning(
                "backtest_rate_limited_retrying",
                symbol=symbol,
                date=str(ist_date),
                attempt=attempt,
                sleep_s=backoff,
            )
            time.sleep(backoff)
        try:
            resp = session.api.getCandleData(params)
        except Exception as e:
            msg = str(e)
            if "exceeding access rate" in msg or "rate" in msg.lower():
                last_err = msg
                continue
            raise RuntimeError(f"getCandleData exception for {symbol} {ist_date}: {msg}") from e
        time.sleep(RATE_LIMIT_SLEEP_S)
        if isinstance(resp, dict) and resp.get("status"):
            rows = resp.get("data") or []
            save_cached_raw(symbol, ist_date, resp)
            log.info("backtest_fetched", symbol=symbol, date=str(ist_date), rows=len(rows))
            return list(rows)
        msg_raw = resp.get("message") if isinstance(resp, dict) else str(resp)[:200]
        last_err = str(msg_raw) if msg_raw is not None else "unknown"
        if "rate" in last_err.lower() or "access denied" in last_err.lower():
            continue
        raise RuntimeError(f"getCandleData failed for {symbol} {ist_date}: {last_err}")

    raise RuntimeError(
        f"getCandleData rate-limited beyond retries for {symbol} {ist_date}: {last_err}"
    )


def raw_rows_to_bars(symbol: str, rows: list[list[Any]]) -> list[Bar]:
    """Convert ['2026-05-20T09:15:00+05:30', o, h, l, c, v] → Bar."""
    bars: list[Bar] = []
    for row in rows:
        ts_str, o, h, low, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
        # SmartAPI returns IST-tz-aware ISO strings.
        open_ist = datetime.fromisoformat(ts_str)
        if open_ist.tzinfo is None:
            open_ist = open_ist.replace(tzinfo=IST)
        open_utc = open_ist.astimezone(UTC)
        bars.append(
            Bar(
                symbol=symbol,
                open_time=open_utc,
                close_time=open_utc + timedelta(minutes=1),
                open=float(o),
                high=float(h),
                low=float(low),
                close=float(c),
                volume=int(v),
            )
        )
    return bars


def list_trading_dates_back(end_ist: date, count: int) -> list[date]:
    """Return `count` trading days ending on or before `end_ist`, oldest first."""
    out: list[date] = []
    d = end_ist
    while len(out) < count and d > end_ist - timedelta(days=count * 3):
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def fetch_universe_for_dates(
    session: _SessionAPI,
    ist_dates: list[date],
    universe: list[str] | None = None,
    token_map: dict[str, str] | None = None,
) -> dict[date, dict[str, list[Bar]]]:
    """Fetch all (date, symbol) pairs, returning bars per date per symbol.

    Skips fetch for any cached pair so re-runs are free.
    """
    universe = universe or NIFTY_5_UNIVERSE
    token_map = token_map or HARDCODED_NSE_TOKENS

    out: dict[date, dict[str, list[Bar]]] = {}
    for d in ist_dates:
        out[d] = {}
        for symbol in universe:
            token = token_map[symbol]
            rows = fetch_day_raw(session, symbol, token, d)
            out[d][symbol] = raw_rows_to_bars(symbol, rows)
    return out
