from __future__ import annotations

from pydantic import ValidationError

from triak_trade.config.settings import Settings


def test_settings_defaults_load() -> None:
    settings = Settings()
    assert settings.APP_NAME == "Triak_Trade"
    assert settings.EXECUTION_MODE in {"demo", "paper"}
    assert settings.BACKTEST_SYNTHETIC_STOP_MAX_LOSS_PCT_OF_BALANCE == 5


def test_live_execution_mode_is_allowed() -> None:
    settings = Settings(EXECUTION_MODE="live")
    assert settings.EXECUTION_MODE == "live"


def test_invalid_execution_mode_is_rejected() -> None:
    try:
        Settings(EXECUTION_MODE="production")
    except ValidationError as exc:
        assert "not valid" in str(exc)
    else:
        raise AssertionError("Expected ValidationError")


def test_admin_ids_are_parsed() -> None:
    settings = Settings(ADMIN_USER_IDS="1, 2,3")
    assert settings.ADMIN_USER_IDS == [1, 2, 3]


def test_api_keys_are_parsed() -> None:
    settings = Settings(GEMINI_API_KEYS="a,b", GROQ_API_KEYS="x, y")
    assert [s.get_secret_value() for s in settings.GEMINI_API_KEYS] == ["a", "b"]
    assert [s.get_secret_value() for s in settings.GROQ_API_KEYS] == ["x", "y"]


def test_telegram_live_channels_are_parsed() -> None:
    settings = Settings(TELEGRAM_LIVE_CHANNELS="a, b,https://t.me/Tofan_Trade")
    assert settings.TELEGRAM_LIVE_CHANNELS == ["a", "b", "https://t.me/Tofan_Trade"]


def test_admin_telegram_usernames_are_parsed() -> None:
    settings = Settings(ADMIN_TELEGRAM_USERNAMES="@A, b")
    assert settings.ADMIN_TELEGRAM_USERNAMES == ["@A", "b"]
