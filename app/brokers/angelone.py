from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, get_settings

TOKEN_CACHE: Path = PROJECT_ROOT / "data" / "tokens.json"

# Angel One sessions are 1-day TTL in practice; we surface a warning a couple of
# hours before expiry so a re-auth can be done before the market closes.
SESSION_TTL = timedelta(hours=22)


@dataclass(frozen=True, slots=True)
class AuthTokens:
    jwt_token: str
    refresh_token: str
    feed_token: str
    client_code: str
    issued_at: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) - self.issued_at > SESSION_TTL


def save_tokens(tokens: AuthTokens) -> None:
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(
        json.dumps(
            {
                "jwt_token": tokens.jwt_token,
                "refresh_token": tokens.refresh_token,
                "feed_token": tokens.feed_token,
                "client_code": tokens.client_code,
                "issued_at": tokens.issued_at.isoformat(),
            },
            indent=2,
        )
    )
    TOKEN_CACHE.chmod(0o600)


def load_tokens() -> AuthTokens:
    if not TOKEN_CACHE.exists():
        raise FileNotFoundError(
            f"Auth tokens missing at {TOKEN_CACHE}. Run `uv run python -m app.scripts.auth` first."
        )
    raw = json.loads(TOKEN_CACHE.read_text())
    return AuthTokens(
        jwt_token=raw["jwt_token"],
        refresh_token=raw["refresh_token"],
        feed_token=raw["feed_token"],
        client_code=raw["client_code"],
        issued_at=datetime.fromisoformat(raw["issued_at"]),
    )


def _stub_legacy_smartapi() -> None:
    """Replace SmartApi's legacy v1 WebSocket modules with empty stubs.

    Their `__init__.py` unconditionally imports `smartApiWebsocket`, which pulls
    in autobahn + twisted (~12 MB of deps we never use — we use SmartWebSocketV2).
    Insert sentinel modules into `sys.modules` before SmartApi loads so its init
    succeeds without those packages installed.
    """
    import sys
    import types

    for legacy in ("SmartApi.smartApiWebsocket", "SmartApi.webSocket"):
        if legacy not in sys.modules:
            stub = types.ModuleType(legacy)
            stub.SmartWebSocket = object  # type: ignore[attr-defined]
            sys.modules[legacy] = stub


def build_smart_connect() -> Any:
    """Return a SmartConnect instance constructed from the .env API key.

    Kept as a lazy import so unit tests that don't touch the SDK don't pay the
    import cost (smartapi-python brings in `requests`, `logzero`, etc.).
    """
    _stub_legacy_smartapi()
    from SmartApi import SmartConnect

    settings = get_settings()
    if not settings.angelone_api_key:
        raise RuntimeError("ANGELONE_API_KEY is not set in .env")
    return SmartConnect(api_key=settings.angelone_api_key)
