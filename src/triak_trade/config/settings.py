"""Application settings."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
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
    EXECUTION_MODE: Literal["backtest", "paper", "demo", "live"] = "demo"
    TOOBIT_BASE_URL: str = "https://api.toobit.com"
    TOOBIT_KLINES_PATH: str = "/quote/v1/klines"
    TOOBIT_FUTURES_MARK_PRICE_KLINES_PATH: str = "/quote/v1/markPrice/klines"
    TOOBIT_FUTURES_INDEX_KLINES_PATH: str = "/quote/v1/index/klines"
    TOOBIT_FUTURES_TICKER_PRICE_PATH: str = "/quote/v1/contract/ticker/price"
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
    BINANCE_PUBLIC_DATA_BASE_URL: str = "https://data.binance.vision"
    BINANCE_FUTURES_REST_BASE_URL: str = "https://fapi.binance.com"
    BINANCE_FUTURES_KLINES_PATH: str = "/fapi/v1/klines"
    BINANCE_FUTURES_TICKER_PRICE_PATH: str = "/fapi/v1/ticker/price"
    BINANCE_PUBLIC_DATA_CACHE_DIR: str = "runtime/cache/binance_public"
    BINANCE_PUBLIC_DATA_TIMEOUT_SECONDS: int = 30
    BINANCE_PUBLIC_REAL_TEST_SYMBOL: str = "BTCUSDT"
    RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS: int = 0
    BACKTEST_MARKET_DATA_PROVIDER: Literal["binance_public", "toobit"] = "binance_public"
    BACKTEST_MARKET_DATA_USE_TOOBIT_FALLBACK: bool = True
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
    # Docker-specific proxy overrides (set in docker-compose environment section)
    TELEGRAM_PROXY_HOST_DOCKER: str = ""
    TELEGRAM_PROXY_PORT_DOCKER: int = 0
    TELEGRAM_HISTORY_BATCH_SIZE: int = 100
    TELEGRAM_LIVE_CHANNELS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    TELEGRAM_REAL_TEST_CHANNEL: str = "https://t.me/Tofan_Trade"
    RUN_TELEGRAM_INTEGRATION_TESTS: int = 0
    TELEGRAM_BOT_TOKEN: SecretStr = Field(default=SecretStr("replace_me"))
    TELEGRAM_LOG_CHANNEL_USERNAME: str = "@triak_logs"
    TELEGRAM_LOG_CHANNEL_ENABLED: bool = False
    TELEGRAM_LOG_CHANNEL_SEND_FULL_TEXT: bool = False
    TELEGRAM_LOG_CHANNEL_MAX_TEXT_CHARS: int = 500
    TELEGRAM_LOG_CHANNEL_PARSE_MODE: str = "HTML"
    TELEGRAM_LOG_CHANNEL_DISABLE_WEB_PAGE_PREVIEW: bool = False
    TELEGRAM_LOG_CHANNEL_SEND_RETRIES: int = 2
    TELEGRAM_LOG_CHANNEL_RETRY_DELAY_SECONDS: int = 1
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
    ROOT_ENV_FILE: str = ".env.local"
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
    AI_CLASSIFIER_ENABLED: bool = False
    AI_CLASSIFIER_MIN_CONFIDENCE: Decimal = Decimal("0.70")
    AI_CLASSIFIER_USE_REGEX_FALLBACK: bool = False
    AI_CLASSIFIER_STORE_PROMPT_TEXT: bool = False
    AI_CLASSIFIER_STORE_RESPONSE_TEXT: bool = False
    AI_CLASSIFIER_FORCE_INCLUDE_KEYWORDS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "short",
            "long",
            "buy",
            "sell",
            "entry",
            "target",
            "sl",
            "tp",
            "stop",
            "leverage",
            "market",
            "limit",
            "شورت",
            "لانگ",
            "خرید",
            "فروش",
            "ورود",
            "تارگت",
            "حد ضرر",
            "استاپ",
        ]
    )
    AI_CLASSIFIER_SKIP_KEYWORDS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["analysis"]
    )
    AI_GATEWAY_ENABLED: bool = False
    AI_GATEWAY_BASE_URL: str = "http://127.0.0.1:8090"
    AI_GATEWAY_TIMEOUT_SECONDS: int = 60
    AI_GATEWAY_PROVIDER_PRIORITY: str = "gemini,groq"
    AI_GATEWAY_DEFAULT_MODEL: str = ""
    AI_GATEWAY_CLASSIFY_PATH: str = "/v1/chat/completions"
    AI_GATEWAY_AUTH_HEADER_NAME: str = "x-api-token"
    AI_GATEWAY_AUTH_TOKEN: SecretStr = Field(default=SecretStr(""))
    AI_GATEWAY_TRUST_ENV: bool = False
    AI_GATEWAY_RETRY_ATTEMPTS: int = 5
    AI_GATEWAY_RETRY_BACKOFF_SECONDS: float = 0.75
    AI_GATEWAY_RUNTIME_DIR: str = "runtime/ai_gateway"
    AI_GATEWAY_PID_FILE: str = "runtime/ai_gateway/ai_gateway.pid"
    AI_GATEWAY_STATUS_FILE: str = "runtime/ai_gateway/status.json"
    AI_GATEWAY_LOG_FILE: str = "runtime/ai_gateway/ai_gateway.log"
    AI_GATEWAY_APP_DIR: str = "external/Ajil_Unified_AI_Gateway"
    RUN_AI_GATEWAY_INTEGRATION_TESTS: int = 0

    # Ajil Unified AI Gateway runtime passthrough
    UAG_AUTH_TOKEN: SecretStr = Field(default=SecretStr(""))

    @model_validator(mode="after")
    def docker_url_overrides(self) -> Settings:
        import os
        # Check if running in Docker
        if os.path.exists("/.dockerenv"):
            # Simple override logic for common services
            if "127.0.0.1" in self.AI_GATEWAY_BASE_URL or "localhost" in self.AI_GATEWAY_BASE_URL:
                self.AI_GATEWAY_BASE_URL = "http://ai-gateway:8080"
            if "localhost" in self.DATABASE_URL or "127.0.0.1" in self.DATABASE_URL:
                self.DATABASE_URL = self.DATABASE_URL.replace(
                    "localhost", "mysql"
                ).replace("127.0.0.1", "mysql")
            if "localhost" in self.REDIS_URL or "127.0.0.1" in self.REDIS_URL:
                self.REDIS_URL = self.REDIS_URL.replace("localhost", "redis").replace(
                    "127.0.0.1", "redis"
                )
        return self

    @field_validator("AI_GATEWAY_AUTH_TOKEN", mode="after")
    @classmethod
    def fallback_ai_gateway_token(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        if value.get_secret_value():
            return value
        uag_token = info.data.get("UAG_AUTH_TOKEN")
        if isinstance(uag_token, SecretStr) and uag_token.get_secret_value():
            return uag_token
        return value

    AI_REAL_TEST_CHANNEL: str = "https://t.me/Tofan_Trade"
    TELEGRAM_MEDIA_DOWNLOAD_ENABLED: bool = True
    TELEGRAM_MEDIA_MAX_IMAGES: int = 1
    TELEGRAM_MEDIA_MAX_BYTES: int = 1_500_000
    BACKTEST_DEFAULT_INITIAL_BALANCE: Decimal = Decimal("100")
    BACKTEST_DEFAULT_RISK_PER_TRADE_PCT: Decimal = Decimal("120")
    BACKTEST_MIN_ALLOCATION_PCT: Decimal = Decimal("2")
    BACKTEST_MAX_ALLOCATION_PCT: Decimal = Decimal("20")
    BACKTEST_DEFAULT_INTERVAL: str = "1m"
    BACKTEST_LIFECYCLE_REFRESH_INTERVAL: str = "30m"
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
    REAL_BACKTEST_ACTIVE_SIGNAL_HOURS: int = 0
    REAL_BACKTEST_REPORT_DIR: str = "runtime/reports/backtests"
    REAL_BACKTEST_USE_AI: bool = True
    REAL_BACKTEST_USE_REGEX_FALLBACK: bool = False
    REAL_BACKTEST_SEND_TO_LOG_CHANNEL: bool = True
    REAL_BACKTEST_LOG_PER_MESSAGE: bool = True
    # When a clear follow-up directive (close / risk-free / SL-TP update) cannot
    # be attached via AI id, reply, symbol, or single-active, fall back to the
    # most-recently-updated open signal. The user requires that an explicit
    # "سیو سود کنید" / "کلوز کنید" never be silently dropped, so this is on by
    # default; every such attach is traced as related_resolution=most_recent_followup.
    REAL_BACKTEST_FOLLOWUP_LAST_RESORT_ATTACH: bool = True
    # Margin/notional risk cap only — clamps how much leverage the simulator will
    # apply when sizing a position. It NEVER blocks a signal from opening; a signal
    # whose leverage exceeds this is opened with leverage clamped to this ceiling.
    BACKTEST_MAX_EFFECTIVE_LEVERAGE: int = 50
    # Leverage assumed when a signal carries no explicit leverage field.
    # Set to 50 so unlevered crypto futures signals get realistic sizing;
    # always capped by BACKTEST_MAX_EFFECTIVE_LEVERAGE.
    BACKTEST_DEFAULT_SIGNAL_LEVERAGE: int = 50
    # When a backtest OPEN signal carries no stop_loss, the simulator synthesizes a
    # stop this many percent away from entry (by side) so risk-per-trade sizing can
    # still size the position. The signal opens and is tracked normally.
    BACKTEST_DEFAULT_STOP_PCT: Decimal = Decimal("5")
    # Cap the worst-case net loss for synthetic stop-loss positions to this percent
    # of the account balance that existed when the position was opened. The cap is
    # enforced after allocation/leverage sizing and includes modeled entry/exit fees.
    BACKTEST_SYNTHETIC_STOP_MAX_LOSS_PCT_OF_BALANCE: Decimal = Decimal("5")
    # Per-side trading fee charged on entry and on each (partial) exit, as a
    # percent of the filled notional (e.g. Decimal("0.04") = 0.04% taker fee).
    # Default 0 keeps PnL gross (no behavior change); set it to model realistic
    # exchange costs. Fees are subtracted from each trade's net pnl and balance.
    BACKTEST_FEE_RATE_PCT: Decimal = Decimal("0")
    # During message-by-message classification, the live simulation is re-run
    # from scratch on every N-th message (or immediately on every signal-bearing
    # message). Higher values reduce CPU/I/O at the cost of slightly delayed
    # dashboard updates for non-signal messages. Default 10 means at most 1/10
    # of the O(N²) cost for plain IGNORE/UNKNOWN messages.
    REAL_BACKTEST_LIVE_SIM_UPDATE_EVERY_N: int = 10
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

    # ── Toobit Futures Trading API ──────────────────────────────────────────
    TOOBIT_FUTURES_ACCOUNT_PATH: str = "/api/v1/contract/account"
    TOOBIT_FUTURES_POSITIONS_PATH: str = "/api/v1/contract/positions"
    TOOBIT_FUTURES_ORDER_PATH: str = "/api/v1/contract/order"
    TOOBIT_FUTURES_CANCEL_ORDER_PATH: str = "/api/v1/contract/cancelOrder"
    TOOBIT_FUTURES_OPEN_ORDERS_PATH: str = "/api/v1/contract/openOrders"
    TOOBIT_FUTURES_LEVERAGE_PATH: str = "/api/v1/contract/leverage"
    TOOBIT_FUTURES_ORDER_TEST_PATH: str = "/api/v1/contract/orderTest"
    TOOBIT_FUTURES_TIMEOUT_SECONDS: int = 20

    # ── Live / Demo Trading ─────────────────────────────────────────────────
    LIVE_TRADING_ENABLED: bool = False
    LIVE_TRADING_MODE: Literal["demo", "live"] = "demo"
    LIVE_TRADING_LIVE_MODE_ENABLED: bool = False
    LIVE_TRADING_RUNTIME_DIR: str = "runtime/live_trading"
    LIVE_TRADING_DEFAULT_INITIAL_BALANCE: Decimal = Decimal("0")
    LIVE_TRADING_DEFAULT_RISK_PER_TRADE_PCT: Decimal = Decimal("120")
    LIVE_TRADING_FEE_RATE_PCT: Decimal = Decimal("0.04")
    LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE: int = 50
    LIVE_TRADING_MAX_CONCURRENT_POSITIONS: int = 10
    LIVE_TRADING_HARD_MAX_RISK_FACTOR_PCT: Decimal = Decimal("120")
    LIVE_TRADING_DEFAULT_SIGNAL_LEVERAGE: int = 50
    LIVE_TRADING_DEFAULT_STOP_PCT: Decimal = Decimal("5")
    LIVE_TRADING_SYNTHETIC_STOP_MAX_LOSS_PCT: Decimal = Decimal("5")
    LIVE_TRADING_MIN_ALLOCATION_PCT: Decimal = Decimal("2")
    LIVE_TRADING_MAX_ALLOCATION_PCT: Decimal = Decimal("20")
    LIVE_TRADING_PRICE_REFRESH_SECONDS: int = 60
    LIVE_TRADING_ACCOUNT_REFRESH_SECONDS: int = 60
    LIVE_TRADING_ORDER_FILL_TIMEOUT_SECONDS: int = 12
    LIVE_TRADING_CLOSE_RECONCILE_ATTEMPTS: int = 3
    LIVE_TRADING_DEFAULT_CHANNELS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    LIVE_TRADING_USE_AI: bool = True
    LIVE_TRADING_REQUIRE_AI_CLASSIFIER: bool = True
    LIVE_TRADING_FAIL_CLOSED_ON_LEVERAGE_SYNC_ERROR: bool = True
    LIVE_TRADING_FAIL_CLOSED_ON_PROTECTION_SYNC_ERROR: bool = True
    LIVE_TRADING_AUTO_RESUME_SESSIONS: bool = True
    LIVE_TRADING_DEFAULT_STRATEGY_KEY: str = "tp_trailing_risk_managed"
    TOOBIT_DEMO_PRIVATE_SYMBOL_MODE: Literal["auto", "tbv_only", "live_only"] = "tbv_only"

    @field_validator("LIVE_TRADING_DEFAULT_CHANNELS", mode="before")
    @classmethod
    def parse_live_channels(cls, value: str | list[str] | None) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [item.strip() for item in value if item.strip()]
        return [item.strip() for item in value.split(",") if item.strip()]

    @field_validator("EXECUTION_MODE", mode="before")
    @classmethod
    def validate_execution_mode(cls, value: str) -> str:
        allowed = {"backtest", "paper", "demo", "live"}
        if value not in allowed:
            msg = f"EXECUTION_MODE='{value}' is not valid. Allowed: {', '.join(sorted(allowed))}."
            raise ValueError(msg)
        return value

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

    @field_validator(
        "AI_CLASSIFIER_FORCE_INCLUDE_KEYWORDS",
        "AI_CLASSIFIER_SKIP_KEYWORDS",
        mode="before",
    )
    @classmethod
    def parse_ai_classifier_keyword_lists(
        cls,
        value: str | list[str] | None,
    ) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [item.strip() for item in value if item and item.strip()]
        return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
