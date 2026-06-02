from __future__ import annotations

from pathlib import Path

from triak_trade.admin_bot.auth import AdminAuthService
from triak_trade.admin_bot.handlers import AdminBotUpdateHandler
from triak_trade.admin_bot.menus import UNAUTHORIZED_TEXT
from triak_trade.admin_bot.state import AdminBotStateStore
from triak_trade.config.settings import Settings


def runtime_settings(tmp_path: Path) -> Settings:
    runtime_dir = tmp_path / "admin_bot"
    return Settings(
        _env_file=None,
        ADMIN_TELEGRAM_USERNAMES=["@we_are_waiting_for_him"],
        TOOBIT_API_KEY="fake-key",
        TOOBIT_API_SECRET="fake-secret",
        TELEGRAM_BOT_TOKEN="fake-token",
        ADMIN_BOT_RUNTIME_DIR=str(runtime_dir),
        ADMIN_BOT_PID_FILE=str(runtime_dir / "admin_bot.pid"),
        ADMIN_BOT_STATUS_FILE=str(runtime_dir / "status.json"),
        ADMIN_BOT_LOG_FILE=str(runtime_dir / "admin_bot.log"),
        ADMIN_BOT_OFFSET_FILE=str(runtime_dir / "update_offset.json"),
    )


def build_handler(tmp_path: Path) -> AdminBotUpdateHandler:
    settings = runtime_settings(tmp_path)
    return AdminBotUpdateHandler(
        settings=settings,
        auth=AdminAuthService(settings.ADMIN_TELEGRAM_USERNAMES),
        state_store=AdminBotStateStore(settings),
    )


def message_update(text: str, username: str = "we_are_waiting_for_him") -> dict[str, object]:
    return {
        "update_id": 10,
        "message": {
            "message_id": 10,
            "text": text,
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 123, "username": username},
        },
    }


def callback_update(data: str, username: str = "we_are_waiting_for_him") -> dict[str, object]:
    return {
        "update_id": 11,
        "callback_query": {
            "id": "11",
            "data": data,
            "from": {"id": 123, "username": username},
            "message": {"message_id": 11, "chat": {"id": 123, "type": "private"}},
        },
    }


def test_start_menu_for_authorized_user(tmp_path: Path) -> None:
    result = build_handler(tmp_path).handle_update(message_update("/start"))

    assert result.authorized is True
    assert result.outgoing
    assert "Triak_Trade" in result.outgoing[0].text
    assert result.outgoing[0].reply_markup is not None


def test_unauthorized_user_gets_persian_rejection(tmp_path: Path) -> None:
    result = build_handler(tmp_path).handle_update(message_update("/start", username="bad_user"))

    assert result.authorized is False
    assert result.outgoing[0].text == UNAUTHORIZED_TEXT


def test_backtest_menu_and_callback(tmp_path: Path) -> None:
    handler = build_handler(tmp_path)

    menu = handler.handle_update(message_update("📊 بک‌تست"))
    run = handler.handle_update(callback_update("backtest:run"))

    assert "بک‌تست" in menu.outgoing[0].text
    assert menu.outgoing[0].reply_markup is not None
    assert "simulation only" in run.outgoing[0].text


def test_system_menu_and_status_do_not_print_secrets(tmp_path: Path) -> None:
    handler = build_handler(tmp_path)

    system = handler.handle_update(message_update("🧪 تست سیستم"))
    toobit = handler.handle_update(message_update("💰 توبیت"))

    text = system.outgoing[0].text + "\n" + toobit.outgoing[0].text
    assert "تست" in system.outgoing[0].text
    assert "api_key_present=True" in toobit.outgoing[0].text
    assert "fake-key" not in text
    assert "fake-secret" not in text
    assert "fake-token" not in text
