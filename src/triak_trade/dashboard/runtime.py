"""Dashboard runtime and CLI helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi.testclient import TestClient

from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app
from triak_trade.dashboard.auth import session_secret_present, token_present
from triak_trade.dashboard.schemas import DashboardRuntimeStatus
from triak_trade.verification.redaction import redact, redact_text


def dashboard_safe_config(settings: Settings) -> dict[str, Any]:
    return {
        "enabled": settings.DASHBOARD_ENABLED,
        "host": settings.DASHBOARD_HOST,
        "port": settings.DASHBOARD_PORT,
        "auth_enabled": settings.DASHBOARD_AUTH_ENABLED,
        "admin_token_present": token_present(settings),
        "session_secret_present": session_secret_present(settings),
        "runtime_dir": settings.DASHBOARD_RUNTIME_DIR,
    }


def run_dashboard(
    settings: Settings,
    *,
    host: str | None = None,
    port: int | None = None,
    reload: bool | None = None,
    max_runtime_seconds: int | None = None,
) -> dict[str, Any]:
    _quiet_external_runtime_loggers()
    actual_host = host or settings.DASHBOARD_HOST
    actual_port = port or settings.DASHBOARD_PORT
    config = uvicorn.Config(
        create_dashboard_app(settings),
        host=actual_host,
        port=actual_port,
        reload=bool(reload if reload is not None else settings.DASHBOARD_AUTO_RELOAD),
        log_level="warning",
    )
    server = uvicorn.Server(config)

    async def serve_bounded() -> None:
        shutdown_task: asyncio.Task[None] | None = None
        if max_runtime_seconds is not None:
            async def stop_later() -> None:
                await asyncio.sleep(max_runtime_seconds)
                server.should_exit = True

            shutdown_task = asyncio.create_task(stop_later())
        await server.serve()
        if shutdown_task is not None:
            shutdown_task.cancel()

    write_dashboard_status(
        settings,
        DashboardRuntimeStatus(
            running=True,
            started_at=datetime.now(timezone.utc),
            last_heartbeat_at=datetime.now(timezone.utc),
            url=f"http://{actual_host}:{actual_port}",
            log_file=settings.DASHBOARD_LOG_FILE,
        ),
    )
    asyncio.run(serve_bounded())
    status = read_dashboard_status(settings)
    status.running = False
    status.last_heartbeat_at = datetime.now(timezone.utc)
    write_dashboard_status(settings, status)
    return {"ran": True, "url": f"http://{actual_host}:{actual_port}", "stopped": True}


def _quiet_external_runtime_loggers() -> None:
    for logger_name in ("telethon", "httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def start_dashboard_process(settings: Settings) -> dict[str, Any]:
    ensure_runtime_dir(settings)
    existing_pid = read_pid(settings)
    if pid_is_running(existing_pid):
        return {"started": False, "already_running": True, "pid": existing_pid}
    executable = shutil.which("triak-trade")
    if executable is None:
        raise RuntimeError("triak-trade executable not found; run editable install first")
    cmd = [
        executable,
        "run-dashboard",
        "--host",
        settings.DASHBOARD_HOST,
        "--port",
        str(settings.DASHBOARD_PORT),
    ]
    log_file = Path(settings.DASHBOARD_LOG_FILE)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handle = log_file.open("a", encoding="utf-8")
    process = subprocess.Popen(cmd, stdout=handle, stderr=handle, start_new_session=True)
    write_pid(settings, process.pid)
    write_dashboard_status(
        settings,
        DashboardRuntimeStatus(
            running=True,
            pid=process.pid,
            started_at=datetime.now(timezone.utc),
            last_heartbeat_at=datetime.now(timezone.utc),
            url=f"http://{settings.DASHBOARD_HOST}:{settings.DASHBOARD_PORT}",
            log_file=settings.DASHBOARD_LOG_FILE,
        ),
    )
    append_log(settings, "dashboard.background_started", {"pid": process.pid})
    return {"started": True, "already_running": False, "pid": process.pid}


def stop_dashboard_process(settings: Settings) -> dict[str, Any]:
    pid = read_pid(settings)
    running_before = pid_is_running(pid)
    if pid is not None and running_before:
        os.kill(pid, signal.SIGTERM)
    remove_pid(settings)
    status = read_dashboard_status(settings)
    status.running = False
    status.pid = None
    status.last_heartbeat_at = datetime.now(timezone.utc)
    write_dashboard_status(settings, status)
    append_log(settings, "dashboard.background_stopped", {"pid": pid})
    return {"stopped": True, "pid": pid, "running_before": running_before}


def dashboard_status(settings: Settings) -> dict[str, Any]:
    status = read_dashboard_status(settings)
    pid = read_pid(settings)
    running = pid_is_running(pid) if pid is not None else status.running
    payload = status.model_dump(mode="json")
    payload["running"] = running
    payload["pid_file_pid"] = pid
    payload["url"] = status.url or f"http://{settings.DASHBOARD_HOST}:{settings.DASHBOARD_PORT}"
    payload["log_file"] = settings.DASHBOARD_LOG_FILE
    return payload


def dashboard_logs(settings: Settings, *, lines: int) -> list[str]:
    path = Path(settings.DASHBOARD_LOG_FILE)
    if not path.exists():
        return []
    return [
        redact_text(line)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    ]


def dashboard_smoke_test(settings: Settings) -> dict[str, Any]:
    app = create_dashboard_app(settings)
    token = settings.DASHBOARD_ADMIN_TOKEN.get_secret_value()
    headers = {"X-Triak-Admin-Token": token}
    with TestClient(app) as client:
        unauthorized = client.get("/", follow_redirects=False)
        authorized = client.get("/", headers=headers)
        backtest = client.post(
            "/backtests/run",
            headers=headers,
            data={
                "channel": settings.BACKTEST_DEFAULT_CHANNEL,
                "interval": "1m",
                "initial_balance": str(settings.BACKTEST_DEFAULT_INITIAL_BALANCE),
                "risk_per_trade_pct": str(settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT),
                "fill_policy": "conservative",
            },
        )
        settings_page = client.get("/settings", headers=headers)
        status_json = client.get("/status", headers=headers)
        status_unauthorized = client.get("/status", follow_redirects=False)
    return {
        "unauthorized_blocked": unauthorized.status_code == 303,
        "dashboard_authorized": authorized.status_code == 200,
        "backtest_fixture_ok": backtest.status_code == 200,
        "settings_ok": settings_page.status_code == 200,
        "status_json_ok": status_json.status_code == 200,
        "status_api_unauthorized": status_unauthorized.status_code == 401,
        "secrets_printed": False,
    }


def dashboard_token_hint() -> str:
    return "DASHBOARD_ADMIN_TOKEN is in root .env.local"


def ensure_runtime_dir(settings: Settings) -> None:
    Path(settings.DASHBOARD_RUNTIME_DIR).mkdir(parents=True, exist_ok=True)


def read_dashboard_status(settings: Settings) -> DashboardRuntimeStatus:
    path = Path(settings.DASHBOARD_STATUS_FILE)
    if not path.exists():
        return DashboardRuntimeStatus(
            url=f"http://{settings.DASHBOARD_HOST}:{settings.DASHBOARD_PORT}",
            log_file=settings.DASHBOARD_LOG_FILE,
        )
    try:
        return DashboardRuntimeStatus.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return DashboardRuntimeStatus(
            running=False,
            last_error_type=type(exc).__name__,
            last_error_message_redacted="dashboard status parse failed",
            log_file=settings.DASHBOARD_LOG_FILE,
        )


def write_dashboard_status(settings: Settings, status: DashboardRuntimeStatus) -> None:
    ensure_runtime_dir(settings)
    path = Path(settings.DASHBOARD_STATUS_FILE)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(redact(status.model_dump(mode="json")), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp.replace(path)


def append_log(settings: Settings, event: str, payload: dict[str, Any]) -> None:
    ensure_runtime_dir(settings)
    path = Path(settings.DASHBOARD_LOG_FILE)
    line = json.dumps(
        redact(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "payload": payload,
            }
        ),
        sort_keys=True,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_pid(settings: Settings) -> int | None:
    path = Path(settings.DASHBOARD_PID_FILE)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def write_pid(settings: Settings, pid: int) -> None:
    ensure_runtime_dir(settings)
    Path(settings.DASHBOARD_PID_FILE).write_text(str(pid), encoding="utf-8")


def remove_pid(settings: Settings) -> None:
    path = Path(settings.DASHBOARD_PID_FILE)
    if path.exists():
        path.unlink()


def pid_is_running(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
