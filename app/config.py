from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

BrokerMode = Literal["paper", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    broker_mode: BrokerMode = "paper"
    angelone_api_key: str = ""
    angelone_client_code: str = ""
    angelone_mpin: str = ""
    angelone_totp_secret: str = ""

    gemini_api_key: str = ""
    gemini_model_tier1: str = "gemini-2.5-pro"
    gemini_model_tier2: str = "gemini-2.5-flash-lite"
    pretrade_llm_check: bool = True
    respect_regime: bool = True

    capital_inr: float = 50_000.0
    daily_loss_limit_inr: float = 1_500.0
    weekly_loss_limit_inr: float = 3_000.0
    risk_per_trade_pct: float = 1.0
    max_trades_per_day: int = 2

    log_level: str = "INFO"
    log_dir: Path = PROJECT_ROOT / "logs"
    db_path: Path = PROJECT_ROOT / "data" / "journal.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


def get_settings() -> Settings:
    return Settings()
