"""Application settings."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Project settings loaded from environment."""

    model_config = SettingsConfigDict(
        env_file=(".env.local",),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    APP_NAME: str = "Triak_Trade"
    APP_ENV: str = "dev"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "human"] = "human"
    DATABASE_URL: str = "mysql+pymysql://triak:triak_local_password@localhost:3306/triak_trade"
    TEST_DATABASE_URL: str = "mysql+pymysql://triak:triak_local_password@localhost:3306/triak_trade_test"
    REDIS_URL: str = "redis://localhost:6379/0"
    EXECUTION_MODE: Literal["backtest", "paper", "demo"] = "demo"
    TOOBIT_BASE_URL: str = "https://api.toobit.com"
    TOOBIT_API_KEY: SecretStr = Field(default=SecretStr("replace_me"))
    TOOBIT_API_SECRET: SecretStr = Field(default=SecretStr("replace_me"))
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: SecretStr = Field(default=SecretStr("replace_me"))
    TELEGRAM_SESSION_NAME: str = "triak_trade"
    TELEGRAM_BOT_TOKEN: SecretStr = Field(default=SecretStr("replace_me"))
    ADMIN_USER_IDS: list[int] = Field(default_factory=list)
    GEMINI_API_KEYS: list[SecretStr] = Field(default_factory=list)
    GROQ_API_KEYS: list[SecretStr] = Field(default_factory=list)
    SIGNAL_CONSOLIDATION_SECONDS: int = 120
    MAX_RISK_PER_TRADE_PCT: Decimal = Decimal("1.0")
    MAX_DAILY_LOSS_PCT: Decimal = Decimal("3.0")
    MAX_WEEKLY_LOSS_PCT: Decimal = Decimal("6.0")
    MAX_LEVERAGE: int = 5
    REQUIRE_STOP_LOSS: bool = True
    ADMIN_DASHBOARD_TOKEN: SecretStr = Field(default=SecretStr("replace_me"))

    @field_validator("EXECUTION_MODE", mode="before")
    @classmethod
    def reject_live_mode(cls, value: str) -> str:
        if value == "live":
            msg = "EXECUTION_MODE='live' is blocked. Allowed modes: backtest, paper, demo."
            raise ValueError(msg)
        return value

    @field_validator("ADMIN_USER_IDS", mode="before")
    @classmethod
    def parse_admin_user_ids(cls, value: str | list[int] | None) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return value
        return [int(item.strip()) for item in value.split(",") if item.strip()]

    @field_validator("GEMINI_API_KEYS", "GROQ_API_KEYS", mode="before")
    @classmethod
    def parse_secret_list(cls, value: str | list[str] | None) -> list[SecretStr]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [SecretStr(item) for item in value]
        return [SecretStr(item.strip()) for item in value.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
