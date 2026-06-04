"""Application settings."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    TOOBIT_KLINES_PATH: str = "/quote/v1/klines"
    TOOBIT_MARKET_DATA_TIMEOUT_SECONDS: int = 20
    TOOBIT_MARKET_DATA_LIMIT: int = 1000
    TOOBIT_MARKET_DATA_DEFAULT_INTERVAL: str = "1m"
    RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS: int = 0
    TOOBIT_REAL_TEST_SYMBOL: str = "BTCUSDT"
    TOOBIT_RECV_WINDOW: int = 5000
    TOOBIT_SIGNED_TIMEOUT_SECONDS: int = 20
    TOOBIT_TIME_PATH: str = "/api/v1/time"
    TOOBIT_EXCHANGE_INFO_PATH: str = "/api/v1/exchangeInfo"
    TOOBIT_SPOT_ORDER_TEST_PATH: str = "/api/v1/spot/orderTest"
    TOOBIT_SAFE_ACCOUNT_PATH: str = ""
    RUN_TOOBIT_SIGNED_INTEGRATION_TESTS: int = 0
    RUN_TOOBIT_ORDERTEST_INTEGRATION_TESTS: int = 0
    TOOBIT_ORDERTEST_SYMBOL: str = "BTCUSDT"
    TOOBIT_ORDERTEST_SIDE: str = "BUY"
    TOOBIT_ORDERTEST_TYPE: str = "LIMIT"
    TOOBIT_ORDERTEST_QUANTITY: str = ""
    TOOBIT_ORDERTEST_PRICE: str = ""
    TOOBIT_API_KEY: SecretStr = Field(default=SecretStr("replace_me"))
    TOOBIT_API_SECRET: SecretStr = Field(default=SecretStr("replace_me"))
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: SecretStr = Field(default=SecretStr("replace_me"))
    TELEGRAM_STRING_SESSION: SecretStr = Field(default=SecretStr(""))
    TELEGRAM_SESSION_NAME: str = "triak_trade"
    TELEGRAM_SESSION_DIR: str = ".sessions"
    TELEGRAM_PROXY_ENABLED: bool = False
    TELEGRAM_PROXY_TYPE: str = "socks5"
    TELEGRAM_PROXY_HOST: str = ""
    TELEGRAM_PROXY_PORT: int = 0
    TELEGRAM_PROXY_RDNS: bool = True
    TELEGRAM_PROXY_USERNAME: str = ""
    TELEGRAM_PROXY_PASSWORD: SecretStr = Field(default=SecretStr(""))
    TELEGRAM_HISTORY_BATCH_SIZE: int = 100
    TELEGRAM_LIVE_CHANNELS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    TELEGRAM_REAL_TEST_CHANNEL: str = "https://t.me/Tofan_Trade"
    RUN_TELEGRAM_INTEGRATION_TESTS: int = 0
    TELEGRAM_BOT_TOKEN: SecretStr = Field(default=SecretStr("replace_me"))
    ADMIN_TELEGRAM_USERNAMES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["@we_are_waiting_for_him"]
    )
    ADMIN_USER_IDS: Annotated[list[int], NoDecode] = Field(default_factory=list)
    ADMIN_BOT_PARSE_MODE: str = "HTML"
    ADMIN_BOT_DISABLE_WEB_PAGE_PREVIEW: bool = True
    ADMIN_BOT_RUNTIME_ENABLED: bool = False
    ADMIN_BOT_POLL_INTERVAL_SECONDS: int = 2
    ADMIN_BOT_LONG_POLL_TIMEOUT_SECONDS: int = 30
    ADMIN_BOT_SUPERVISOR_RESTART_ON_CRASH: bool = True
    ADMIN_BOT_SUPERVISOR_MAX_RESTARTS: int = 20
    ADMIN_BOT_SUPERVISOR_RESTART_DELAY_SECONDS: int = 3
    ADMIN_BOT_RUNTIME_DIR: str = "runtime/admin_bot"
    ADMIN_BOT_PID_FILE: str = "runtime/admin_bot/admin_bot.pid"
    ADMIN_BOT_STATUS_FILE: str = "runtime/admin_bot/status.json"
    ADMIN_BOT_LOG_FILE: str = "runtime/admin_bot/admin_bot.log"
    ADMIN_BOT_OFFSET_FILE: str = "runtime/admin_bot/update_offset.json"
    RUN_TELEGRAM_BOT_INTEGRATION_TESTS: int = 0
    ADMIN_BOT_TEST_MESSAGE_TEXT: str = "Triak_Trade admin bot test: configuration OK"
    TELEGRAM_LOG_CHANNEL_USERNAME: str = "@triak_logs"
    TELEGRAM_LOG_CHANNEL_ENABLED: bool = False
    TELEGRAM_LOG_CHANNEL_SEND_FULL_TEXT: bool = False
    TELEGRAM_LOG_CHANNEL_MAX_TEXT_CHARS: int = 500
    TELEGRAM_LOG_CHANNEL_PARSE_MODE: str = "HTML"
    TELEGRAM_LOG_CHANNEL_DISABLE_WEB_PAGE_PREVIEW: bool = False
    RUN_TELEGRAM_LOG_CHANNEL_INTEGRATION_TESTS: int = 0
    PROCESSING_AUDIT_ENABLED: bool = True
    PROCESSING_AUDIT_STORE_IN_DB: bool = True
    PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL: bool = False
    DASHBOARD_ENABLED: bool = True
    DASHBOARD_HOST: str = "127.0.0.1"
    DASHBOARD_PORT: int = 8088
    DASHBOARD_AUTH_ENABLED: bool = True
    DASHBOARD_ADMIN_TOKEN: SecretStr = Field(default=SecretStr(""))
    DASHBOARD_SESSION_SECRET: SecretStr = Field(default=SecretStr(""))
    DASHBOARD_RUNTIME_DIR: str = "runtime/dashboard"
    DASHBOARD_PID_FILE: str = "runtime/dashboard/dashboard.pid"
    DASHBOARD_STATUS_FILE: str = "runtime/dashboard/status.json"
    DASHBOARD_LOG_FILE: str = "runtime/dashboard/dashboard.log"
    DASHBOARD_AUTO_RELOAD: bool = False
    AUTO_MODE_ENABLED: bool = False
    AUTO_MODE_SCOPE: str = "demo_only"
    AUTO_MODE_REQUIRE_RISK_ENGINE: bool = True
    AUTO_MODE_REQUIRE_KILL_SWITCH_CLEAR: bool = True
    KILL_SWITCH_ENABLED: bool = False
    KILL_SWITCH_REASON: str = ""
    GEMINI_API_KEYS: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    GROQ_API_KEYS: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    SIGNAL_CONSOLIDATION_SECONDS: int = 180
    SIGNAL_MAX_UPDATE_WINDOW_HOURS: int = 48
    CHANNEL_AGENT_CONTEXT_MESSAGE_LIMIT: int = 50
    AI_CLASSIFIER_FORWARD_CONTEXT_LIMIT: int = 3
    AI_CLASSIFIER_TEXT_PROVIDER: str = "groq"
    AI_CLASSIFIER_TEXT_MODEL: str = "openai/gpt-oss-120b"
    AI_CLASSIFIER_VISION_PROVIDER: str = "gemini"
    AI_CLASSIFIER_VISION_MODEL: str = "gemini-3.1-flash-lite"
    AI_GATEWAY_ENABLED: bool = False
    AI_GATEWAY_BASE_URL: str = "http://127.0.0.1:8090"
    AI_GATEWAY_TIMEOUT_SECONDS: int = 30
    AI_GATEWAY_PROVIDER_PRIORITY: str = "gemini,groq"
    AI_GATEWAY_DEFAULT_MODEL: str = ""
    AI_GATEWAY_CLASSIFY_PATH: str = "/v1/chat/completions"
    AI_GATEWAY_AUTH_HEADER_NAME: str = "x-api-token"
    AI_GATEWAY_AUTH_TOKEN: SecretStr = Field(default=SecretStr(""))
    AI_GATEWAY_TRUST_ENV: bool = False
    AI_GATEWAY_RUNTIME_DIR: str = "runtime/ai_gateway"
    AI_GATEWAY_PID_FILE: str = "runtime/ai_gateway/ai_gateway.pid"
    AI_GATEWAY_STATUS_FILE: str = "runtime/ai_gateway/status.json"
    AI_GATEWAY_LOG_FILE: str = "runtime/ai_gateway/ai_gateway.log"
    AI_GATEWAY_APP_DIR: str = "external/Ajil_Unified_AI_Gateway"
    RUN_AI_GATEWAY_INTEGRATION_TESTS: int = 0
    AI_CLASSIFIER_ENABLED: bool = False
    AI_CLASSIFIER_MIN_CONFIDENCE: Decimal = Decimal("0.70")
    AI_CLASSIFIER_USE_REGEX_FALLBACK: bool = False
    AI_CLASSIFIER_STORE_PROMPT_TEXT: bool = False
    AI_CLASSIFIER_STORE_RESPONSE_TEXT: bool = False
    AI_REAL_TEST_CHANNEL: str = "https://t.me/Tofan_Trade"
    TELEGRAM_MEDIA_DOWNLOAD_ENABLED: bool = True
    TELEGRAM_MEDIA_MAX_IMAGES: int = 1
    TELEGRAM_MEDIA_MAX_BYTES: int = 1_500_000
    BACKTEST_DEFAULT_INITIAL_BALANCE: Decimal = Decimal("1000")
    BACKTEST_DEFAULT_RISK_PER_TRADE_PCT: Decimal = Decimal("1")
    BACKTEST_DEFAULT_INTERVAL: str = "1m"
    BACKTEST_DEFAULT_FILL_POLICY: str = "conservative"
    BACKTEST_MAX_MESSAGES: int = 5000
    BACKTEST_MAX_CANDLES: int = 200000
    BACKTEST_USE_AI_CLASSIFIER: bool = False
    BACKTEST_USE_REGEX_FALLBACK: bool = False
    BACKTEST_DEFAULT_CHANNEL: str = "https://t.me/Tofan_Trade"
    RUN_BACKTEST_INTEGRATION_TESTS: int = 0
    REAL_BACKTEST_ENABLED: bool = False
    REAL_BACKTEST_DEFAULT_CHANNEL: str = "https://t.me/Tofan_Trade"
    REAL_BACKTEST_DEFAULT_LOOKBACK_HOURS: int = 24
    REAL_BACKTEST_DEFAULT_INTERVAL: str = "1m"
    REAL_BACKTEST_MAX_MESSAGES: int = 1000
    REAL_BACKTEST_MAX_CANDLES: int = 100000
    REAL_BACKTEST_REPORT_DIR: str = "runtime/reports/backtests"
    REAL_BACKTEST_USE_AI: bool = True
    REAL_BACKTEST_USE_REGEX_FALLBACK: bool = False
    REAL_BACKTEST_SEND_TO_ADMIN_BOT: bool = True
    REAL_BACKTEST_SEND_TO_LOG_CHANNEL: bool = True
    REAL_BACKTEST_LOG_PER_MESSAGE: bool = False
    VERIFICATION_REPORT_DIR: str = "runtime/reports"
    VERIFICATION_WRITE_JSON: bool = True
    VERIFICATION_WRITE_MARKDOWN: bool = True
    VERIFICATION_INCLUDE_SKIPPED: bool = True
    VERIFICATION_MAX_REAL_TELEGRAM_MESSAGES: int = 5
    VERIFICATION_MAX_REAL_BACKTEST_HOURS: int = 6
    RUN_SYSTEM_REAL_SMOKE_TESTS: int = 0
    RUN_MYSQL_INTEGRATION_TESTS: int = 0
    RUN_REDIS_INTEGRATION_TESTS: int = 0
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
        parsed: list[int] = []
        for item in value.split(","):
            stripped = item.strip()
            if not stripped:
                continue
            if stripped.lstrip("+-").isdigit():
                parsed.append(int(stripped))
        return parsed

    @field_validator("GEMINI_API_KEYS", "GROQ_API_KEYS", mode="before")
    @classmethod
    def parse_secret_list(cls, value: str | list[str] | None) -> list[SecretStr]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [SecretStr(item) for item in value]
        return [SecretStr(item.strip()) for item in value.split(",") if item.strip()]

    @field_validator("TELEGRAM_LIVE_CHANNELS", mode="before")
    @classmethod
    def parse_channel_list(cls, value: str | list[str] | None) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [item.strip() for item in value if item.strip()]
        return [item.strip() for item in value.split(",") if item.strip()]

    @field_validator("ADMIN_TELEGRAM_USERNAMES", mode="before")
    @classmethod
    def parse_admin_usernames(cls, value: str | list[str] | None) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [item.strip() for item in value if item.strip()]
        return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
