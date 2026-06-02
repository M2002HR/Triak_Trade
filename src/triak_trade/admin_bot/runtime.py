"""CLI-facing admin bot runtime orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
from typing import Any

from triak_trade.admin_bot.auth import AdminAuthService
from triak_trade.admin_bot.handlers import AdminBotUpdateHandler
from triak_trade.admin_bot.polling import AdminBotPollingService, TelegramBotPollingClient
from triak_trade.admin_bot.state import AdminBotRuntimeState, AdminBotStateStore, utc_now
from triak_trade.admin_bot.supervisor import AdminBotSupervisor
from triak_trade.config.settings import Settings


def validate_real_admin_bot_runtime(settings: Settings) -> None:
    token = settings.TELEGRAM_BOT_TOKEN.get_secret_value()
    if not settings.ADMIN_BOT_RUNTIME_ENABLED:
        raise RuntimeError("ADMIN_BOT_RUNTIME_ENABLED=true is required for real admin bot runtime")
    if not token or token == "replace_me":
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required for real admin bot runtime")
    if not settings.ADMIN_TELEGRAM_USERNAMES:
        raise RuntimeError("ADMIN_TELEGRAM_USERNAMES is required for real admin bot runtime")


async def run_admin_bot_runtime(
    settings: Settings,
    *,
    real: bool,
    watch: bool,
    once: bool,
    max_runtime_seconds: int | None = None,
) -> dict[str, Any]:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if real:
        validate_real_admin_bot_runtime(settings)
    state_store = AdminBotStateStore(settings)
    state_store.ensure_runtime_dir()
    handler = AdminBotUpdateHandler(
        settings=settings,
        auth=AdminAuthService(settings.ADMIN_TELEGRAM_USERNAMES),
        state_store=state_store,
    )
    client = (
        TelegramBotPollingClient(
            bot_token=settings.TELEGRAM_BOT_TOKEN.get_secret_value(),
            parse_mode=settings.ADMIN_BOT_PARSE_MODE,
            disable_web_preview=settings.ADMIN_BOT_DISABLE_WEB_PAGE_PREVIEW,
            timeout_seconds=settings.ADMIN_BOT_LONG_POLL_TIMEOUT_SECONDS + 5,
        )
        if real
        else None
    )
    service = AdminBotPollingService(
        settings=settings,
        state_store=state_store,
        handler=handler,
        real=real,
        client=client,
    )
    if once:
        result = await service.run_once()
    elif watch:
        supervisor = AdminBotSupervisor(settings=settings, state_store=state_store)
        result = await supervisor.run(
            lambda: service.run_loop(max_runtime_seconds=max_runtime_seconds),
            max_runtime_seconds=max_runtime_seconds,
        )
    else:
        result = await service.run_loop(max_runtime_seconds=max_runtime_seconds)
    result["real"] = real
    result["watch"] = watch
    result["once"] = once
    result["status_file"] = settings.ADMIN_BOT_STATUS_FILE
    result["log_file"] = settings.ADMIN_BOT_LOG_FILE
    return result


def run_admin_bot_sync(
    settings: Settings,
    *,
    real: bool,
    watch: bool,
    once: bool,
    max_runtime_seconds: int | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        run_admin_bot_runtime(
            settings,
            real=real,
            watch=watch,
            once=once,
            max_runtime_seconds=max_runtime_seconds,
        )
    )


def start_admin_bot_process(settings: Settings, *, real: bool, watch: bool) -> dict[str, Any]:
    if real:
        validate_real_admin_bot_runtime(settings)
    store = AdminBotStateStore(settings)
    store.ensure_runtime_dir()
    existing_pid = store.read_pid()
    if store.pid_is_running(existing_pid):
        return {"started": False, "already_running": True, "pid": existing_pid}
    if existing_pid is not None:
        store.remove_pid()

    executable = shutil.which("triak-trade")
    if executable is None:
        raise RuntimeError("triak-trade executable not found; run editable install first")
    cmd = [executable, "run-admin-bot"]
    if real:
        cmd.append("--real")
    if watch:
        cmd.append("--watch")

    log_path = store.log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")
    process = subprocess.Popen(
        cmd,
        stdout=log_handle,
        stderr=log_handle,
        start_new_session=True,
    )
    store.write_pid(process.pid)
    state = store.read_status()
    state.running = True
    state.pid = process.pid
    state.started_at = utc_now()
    state.last_heartbeat_at = utc_now()
    state.mode = "real" if real else "fake"
    state.watch = watch
    store.write_status(state)
    store.append_log(
        "admin_bot.background_started",
        {"pid": process.pid, "real": real, "watch": watch},
    )
    return {"started": True, "already_running": False, "pid": process.pid}


def stop_admin_bot_process(settings: Settings) -> dict[str, Any]:
    store = AdminBotStateStore(settings)
    pid = store.read_pid()
    running_before = store.pid_is_running(pid)
    if pid is not None and running_before:
        os.kill(pid, signal.SIGTERM)
    store.remove_pid()
    state = store.read_status()
    state.running = False
    state.pid = None
    state.last_heartbeat_at = utc_now()
    store.write_status(state)
    store.append_log("admin_bot.background_stopped", {"pid": pid, "running_before": running_before})
    return {"stopped": True, "pid": pid, "running_before": running_before}


def get_admin_bot_status(settings: Settings) -> dict[str, Any]:
    store = AdminBotStateStore(settings)
    state = store.read_status()
    pid = store.read_pid()
    running = store.pid_is_running(pid) if pid is not None else state.running
    payload = state.model_dump(mode="json")
    payload["pid_file_pid"] = pid
    payload["running"] = running
    payload["status_file"] = settings.ADMIN_BOT_STATUS_FILE
    payload["log_file"] = settings.ADMIN_BOT_LOG_FILE
    payload["offset_file"] = settings.ADMIN_BOT_OFFSET_FILE
    return payload


def tail_admin_bot_logs(settings: Settings, *, lines: int) -> list[str]:
    return AdminBotStateStore(settings).tail_logs(lines)


def run_admin_bot_smoke_test(settings: Settings) -> dict[str, Any]:
    store = AdminBotStateStore(settings)
    store.ensure_runtime_dir()
    state = AdminBotRuntimeState(
        running=False,
        pid=None,
        started_at=utc_now(),
        last_heartbeat_at=utc_now(),
        mode="fake-smoke",
    )
    store.write_status(state)
    handler = AdminBotUpdateHandler(
        settings=settings,
        auth=AdminAuthService(settings.ADMIN_TELEGRAM_USERNAMES),
        state_store=store,
    )
    updates = [
        _message_update(10, "/start", "we_are_waiting_for_him", 1001),
        _message_update(11, "/start", "not_allowed", 1002),
        _callback_update(12, "backtest:run", "we_are_waiting_for_him", 1001),
        _message_update(13, "💰 توبیت", "we_are_waiting_for_him", 1001),
    ]
    handled = [handler.handle_update(update) for update in updates]
    for item in handled:
        store.append_log(
            "admin_bot.smoke_update",
            {
                "update_id": item.update_id,
                "authorized": item.authorized,
                "outgoing_count": len(item.outgoing),
            },
        )
    return {
        "mode": "fake-smoke",
        "handled_updates": len(handled),
        "authorized_updates": sum(1 for item in handled if item.authorized),
        "unauthorized_updates": sum(1 for item in handled if not item.authorized),
        "outgoing_messages": sum(len(item.outgoing) for item in handled),
        "contains_unauthorized_rejection": any(
            "مجاز" in message.text for item in handled for message in item.outgoing
        ),
        "status": get_admin_bot_status(settings),
    }


def dump_json(payload: dict[str, Any] | list[Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def _message_update(update_id: int, text: str, username: str, chat_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": update_id,
            "text": text,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "username": username},
        },
    }


def _callback_update(update_id: int, data: str, username: str, chat_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "data": data,
            "from": {"id": chat_id, "is_bot": False, "username": username},
            "message": {"message_id": update_id, "chat": {"id": chat_id, "type": "private"}},
        },
    }
