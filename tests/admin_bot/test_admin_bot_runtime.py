from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from triak_trade.admin_bot.auth import AdminAuthService
from triak_trade.admin_bot.handlers import AdminBotUpdateHandler
from triak_trade.admin_bot.polling import AdminBotPollingService
from triak_trade.admin_bot.runtime import (
    run_admin_bot_smoke_test,
    start_admin_bot_process,
    stop_admin_bot_process,
    validate_real_admin_bot_runtime,
)
from triak_trade.admin_bot.state import AdminBotRuntimeState, AdminBotStateStore
from triak_trade.admin_bot.supervisor import AdminBotSupervisor
from triak_trade.config.settings import Settings


def runtime_settings(tmp_path: Path) -> Settings:
    runtime_dir = tmp_path / "admin_bot"
    return Settings(
        _env_file=None,
        ADMIN_TELEGRAM_USERNAMES=["@we_are_waiting_for_him"],
        TELEGRAM_BOT_TOKEN="replace_me",
        ADMIN_BOT_RUNTIME_ENABLED=False,
        ADMIN_BOT_POLL_INTERVAL_SECONDS=1,
        ADMIN_BOT_SUPERVISOR_RESTART_DELAY_SECONDS=0,
        ADMIN_BOT_RUNTIME_DIR=str(runtime_dir),
        ADMIN_BOT_PID_FILE=str(runtime_dir / "admin_bot.pid"),
        ADMIN_BOT_STATUS_FILE=str(runtime_dir / "status.json"),
        ADMIN_BOT_LOG_FILE=str(runtime_dir / "admin_bot.log"),
        ADMIN_BOT_OFFSET_FILE=str(runtime_dir / "update_offset.json"),
    )


def test_runtime_settings_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)
    assert settings.ADMIN_BOT_RUNTIME_ENABLED is False
    assert settings.ADMIN_BOT_RUNTIME_DIR == "runtime/admin_bot"


def test_real_runtime_guard_blocks_without_enabled_flag(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    with pytest.raises(RuntimeError, match="ADMIN_BOT_RUNTIME_ENABLED"):
        validate_real_admin_bot_runtime(settings)


def test_state_store_writes_status_offset_and_redacted_logs(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    store = AdminBotStateStore(settings)
    store.write_status(AdminBotRuntimeState(running=True, handled_updates_count=2))
    store.write_offset(42)
    store.append_log("secret_check", {"telegram_bot_token": "123456:abcdefabcdefabcdefabcdef"})

    assert store.read_status().running is True
    assert store.read_status().handled_updates_count == 2
    assert store.read_offset() == 42
    log_text = "\n".join(store.tail_logs(10))
    assert "***REDACTED***" in log_text
    assert "123456:abcdef" not in log_text


@pytest.mark.asyncio
async def test_fake_polling_run_once_updates_status_and_offset(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    store = AdminBotStateStore(settings)
    handler = AdminBotUpdateHandler(
        settings=settings,
        auth=AdminAuthService(settings.ADMIN_TELEGRAM_USERNAMES),
        state_store=store,
    )
    service = AdminBotPollingService(
        settings=settings,
        state_store=store,
        handler=handler,
        real=False,
    )

    result = await service.run_once()

    assert result["handled_updates"] == 1
    assert store.read_offset() == 2
    assert store.read_status().handled_updates_count == 1
    assert "welcome" not in "\n".join(store.tail_logs(20)).lower()


@pytest.mark.asyncio
async def test_supervisor_restarts_after_crash(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    store = AdminBotStateStore(settings)
    supervisor = AdminBotSupervisor(settings=settings, state_store=store)
    calls = 0

    async def flaky() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")
        return {"ok": True}

    result = await supervisor.run(flaky)

    assert result["ok"] is True
    assert result["restart_count"] == 1
    assert store.read_status().restart_count == 1


def test_start_duplicate_prevention_and_stop_are_safe(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    store = AdminBotStateStore(settings)
    store.write_pid(os.getpid())

    started = start_admin_bot_process(settings, real=False, watch=True)

    assert started["already_running"] is True

    store.remove_pid()
    stopped = stop_admin_bot_process(settings)
    assert stopped["stopped"] is True


def test_admin_bot_smoke_test_uses_fake_updates(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)

    result = run_admin_bot_smoke_test(settings)

    assert result["mode"] == "fake-smoke"
    assert result["handled_updates"] == 4
    assert result["unauthorized_updates"] == 1
    assert result["contains_unauthorized_rejection"] is True


def test_fake_watch_loop_is_bounded(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    store = AdminBotStateStore(settings)
    handler = AdminBotUpdateHandler(
        settings=settings,
        auth=AdminAuthService(settings.ADMIN_TELEGRAM_USERNAMES),
        state_store=store,
    )
    service = AdminBotPollingService(
        settings=settings,
        state_store=store,
        handler=handler,
        real=False,
    )

    result = asyncio.run(service.run_loop(max_runtime_seconds=1))

    assert result["cycles"] >= 1
    assert result["handled_updates"] >= 1
