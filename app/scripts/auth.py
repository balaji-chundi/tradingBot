"""Angel One SmartAPI login + symbol-token cache builder.

Usage:
    uv run python -m app.scripts.auth

Saves two files into data/:
    - tokens.json          (jwt + refresh + feed_token, chmod 600)
    - symbol_tokens.json   (tradingsymbol -> symboltoken for the universe)

Re-run each market morning before `make run`. Angel One sessions expire daily.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import Any

import pyotp
import structlog

from app.brokers.angelone import AuthTokens, build_smart_connect, save_tokens
from app.config import get_settings
from app.strategy.universe import (
    EXCHANGE_NSE_CM,
    HARDCODED_NSE_TOKENS,
    NIFTY_5_UNIVERSE,
    save_token_map,
)

DORMANT_ERRORCODE = "AB2000"

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = structlog.get_logger()


def _require_credentials() -> None:
    s = get_settings()
    missing = [
        name
        for name, value in (
            ("ANGELONE_API_KEY", s.angelone_api_key),
            ("ANGELONE_CLIENT_CODE", s.angelone_client_code),
            ("ANGELONE_MPIN", s.angelone_mpin),
            ("ANGELONE_TOTP_SECRET", s.angelone_totp_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing creds in .env: {', '.join(missing)}")


def login_and_cache_tokens() -> tuple[Any, AuthTokens]:
    """Generate a fresh session via TOTP and persist tokens to disk.

    Returns the authenticated SmartConnect client alongside the tokens so the
    caller can reuse the same session for subsequent REST calls (searchScrip
    etc.) without re-authenticating.
    """
    settings = get_settings()
    _require_credentials()

    api = build_smart_connect()
    totp_code = pyotp.TOTP(settings.angelone_totp_secret).now()

    log.info("login_attempt", client=settings.angelone_client_code)
    session: dict[str, Any] = api.generateSession(
        settings.angelone_client_code,
        settings.angelone_mpin,
        totp_code,
    )
    if not session.get("status"):
        raise RuntimeError(
            f"Login failed: {session.get('message')} (errorcode={session.get('errorcode')})"
        )

    feed_token: str = api.getfeedToken()
    data = session["data"]
    tokens = AuthTokens(
        jwt_token=data["jwtToken"],
        refresh_token=data["refreshToken"],
        feed_token=feed_token,
        client_code=settings.angelone_client_code,
        issued_at=datetime.now(UTC),
    )
    save_tokens(tokens)
    log.info("login_ok", client=tokens.client_code)
    return api, tokens


def fetch_and_cache_universe_tokens(api: Any) -> dict[str, str]:
    """Look up NSE symboltokens for the universe via searchScrip.

    Falls back to the verified [[hardcoded-nse-tokens]] map when Angel One
    rejects searchScrip with AB2000 (demat dormant). The fallback is fine
    because WebSocket V2 market data is unaffected by dormancy; only the
    order-side endpoints (incl. searchScrip) are blocked.
    """
    mapping: dict[str, str] = {}
    for symbol in NIFTY_5_UNIVERSE:
        try:
            result = api.searchScrip(exchange="NSE", searchscrip=symbol)
        except Exception as e:
            log.warning("searchScrip_error_falling_back", symbol=symbol, error=str(e))
            result = None
        if isinstance(result, dict) and result.get("errorcode") == DORMANT_ERRORCODE:
            log.warning(
                "searchScrip_blocked_by_dormancy_falling_back",
                symbol=symbol,
                message=result.get("message"),
            )
            mapping = dict(HARDCODED_NSE_TOKENS)
            save_token_map(mapping)
            return mapping
        match = _select_match(result, symbol)
        if not match:
            raise RuntimeError(f"searchScrip returned no NSE EQ match for {symbol}: {result}")
        mapping[symbol] = str(match["symboltoken"])
        log.info("token_resolved", symbol=symbol, token=mapping[symbol])
    save_token_map(mapping)
    return mapping


def _select_match(result: Any, symbol: str) -> dict[str, Any] | None:
    """Pick the exact tradingsymbol+EQ match out of the searchScrip payload."""
    if not isinstance(result, dict) or not result.get("status"):
        return None
    rows = result.get("data") or []
    for row in rows:
        if row.get("tradingsymbol") == symbol and row.get("exchange") == "NSE":
            return row  # type: ignore[no-any-return]
    return None


def main() -> None:
    api, tokens = login_and_cache_tokens()
    mapping = fetch_and_cache_universe_tokens(api)
    log.info(
        "auth_done",
        client=tokens.client_code,
        symbols=len(mapping),
        exchange_type=EXCHANGE_NSE_CM,
    )


if __name__ == "__main__":
    main()
