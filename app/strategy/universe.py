from __future__ import annotations

import json
from pathlib import Path

from app.config import PROJECT_ROOT

NIFTY_5_UNIVERSE: list[str] = [
    "RELIANCE-EQ",
    "HDFCBANK-EQ",
    "ICICIBANK-EQ",
    "INFY-EQ",
    "TCS-EQ",
]

EXCHANGE_NSE_CM = 1  # SmartWebSocketV2 exchangeType for NSE Cash Market

SYMBOL_TOKEN_CACHE: Path = PROJECT_ROOT / "data" / "symbol_tokens.json"


def load_token_map() -> dict[str, str]:
    """Return {tradingsymbol: symboltoken} populated by scripts.auth.

    Raises FileNotFoundError with a helpful message if the cache isn't on disk
    yet — that's a signal the user hasn't run the auth/setup CLI for the day.
    """
    if not SYMBOL_TOKEN_CACHE.exists():
        raise FileNotFoundError(
            f"Symbol token cache missing at {SYMBOL_TOKEN_CACHE}. "
            "Run `uv run python -m app.scripts.auth` first."
        )
    data = json.loads(SYMBOL_TOKEN_CACHE.read_text())
    missing = [s for s in NIFTY_5_UNIVERSE if s not in data]
    if missing:
        raise RuntimeError(f"Token cache missing symbols: {missing}")
    return {symbol: str(data[symbol]) for symbol in NIFTY_5_UNIVERSE}


def save_token_map(mapping: dict[str, str]) -> None:
    SYMBOL_TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SYMBOL_TOKEN_CACHE.write_text(json.dumps(mapping, indent=2))
