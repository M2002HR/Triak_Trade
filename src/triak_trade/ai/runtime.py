"""Local Ajil gateway runtime helpers."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from triak_trade.config.settings import Settings
from triak_trade.verification.redaction import redact, redact_text


def ai_gateway_safe_config(settings: Settings) -> dict[str, Any]:
    parsed = urlparse(settings.AI_GATEWAY_BASE_URL)
    return {
        "enabled": settings.AI_GATEWAY_ENABLED,
        "base_url": settings.AI_GATEWAY_BASE_URL,
        "host": parsed.hostname or "",
        "port": parsed.port or 80,
        "classify_path": settings.AI_GATEWAY_CLASSIFY_PATH,
        "default_model_present": bool(settings.AI_GATEWAY_DEFAULT_MODEL.strip()),
        "provider_priority": [
            item.strip()
            for item in settings.AI_GATEWAY_PROVIDER_PRIORITY.split(",")
            if item.strip()
        ],
        "auth_header_name": settings.AI_GATEWAY_AUTH_HEADER_NAME,
        "auth_token_present": bool(settings.AI_GATEWAY_AUTH_TOKEN.get_secret_value().strip()),
        "trust_env": settings.AI_GATEWAY_TRUST_ENV,
        "runtime_dir": settings.AI_GATEWAY_RUNTIME_DIR,
        "app_dir": settings.AI_GATEWAY_APP_DIR,
    }


def start_ai_gateway_process(settings: Settings) -> dict[str, Any]:
    ensure_runtime_dir(settings)
    existing_pid = read_pid(settings)
    if pid_is_running(existing_pid):
        return {"started": False, "already_running": True, "pid": existing_pid}

    gateway_dir = Path(settings.AI_GATEWAY_APP_DIR).resolve()
    if not gateway_dir.exists():
        raise RuntimeError("Ajil gateway app directory is missing")

    parsed = _parse_local_base_url(settings.AI_GATEWAY_BASE_URL)
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "unified_gateway.app.main:app",
        "--host",
        parsed["host"],
        "--port",
        str(parsed["port"]),
        "--log-level",
        "warning",
    ]
    env = _build_runtime_env(settings, host=parsed["host"], port=parsed["port"])
    log_file = Path(settings.AI_GATEWAY_LOG_FILE)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handle = log_file.open("a", encoding="utf-8")
    process = subprocess.Popen(
        command,
        stdout=handle,
        stderr=handle,
        start_new_session=True,
        env=env,
    )
    write_pid(settings, process.pid)
    write_status(
        settings,
        {
            "running": True,
            "pid": process.pid,
            "base_url": settings.AI_GATEWAY_BASE_URL,
            "log_file": settings.AI_GATEWAY_LOG_FILE,
            "started_at": time.time(),
            "last_error": None,
        },
    )
    if not wait_for_gateway_ready(settings, timeout_seconds=20):
        stop_ai_gateway_process(settings)
        raise RuntimeError("Ajil gateway failed readiness check; inspect ai-gateway-logs")
    append_log(
        settings,
        "ai_gateway.started",
        {"pid": process.pid, "base_url": settings.AI_GATEWAY_BASE_URL},
    )
    return {
        "started": True,
        "already_running": False,
        "pid": process.pid,
        "base_url": settings.AI_GATEWAY_BASE_URL,
    }


def ensure_local_ai_gateway_ready(settings: Settings) -> dict[str, Any]:
    if not settings.AI_GATEWAY_ENABLED:
        return {"enabled": False, "managed": False, "running": False}

    import os
    if os.path.exists("/.dockerenv"):
        return {
            "enabled": True,
            "managed": False,
            "running": wait_for_gateway_ready(settings, timeout_seconds=2),
        }

    raw = urlparse(settings.AI_GATEWAY_BASE_URL)
    host = raw.hostname or "127.0.0.1"
    if host not in {"127.0.0.1", "localhost"}:
        return {
            "enabled": True,
            "managed": False,
            "running": wait_for_gateway_ready(settings, timeout_seconds=2),
        }
    _parse_local_base_url(settings.AI_GATEWAY_BASE_URL)
    if wait_for_gateway_ready(settings, timeout_seconds=2):
        return {"enabled": True, "managed": True, "running": True, "started": False}
    started = start_ai_gateway_process(settings)
    return {
        "enabled": True,
        "managed": True,
        "running": True,
        "started": bool(started.get("started")),
        "pid": started.get("pid"),
    }


def stop_ai_gateway_process(settings: Settings) -> dict[str, Any]:
    pid = read_pid(settings)
    running_before = pid_is_running(pid)
    if pid is not None and running_before:
        os.kill(pid, signal.SIGTERM)
    remove_pid(settings)
    status = read_status(settings)
    status["running"] = False
    status["pid"] = None
    write_status(settings, status)
    append_log(settings, "ai_gateway.stopped", {"pid": pid})
    return {"stopped": True, "pid": pid, "running_before": running_before}


def ai_gateway_status(settings: Settings) -> dict[str, Any]:
    status = read_status(settings)
    pid = read_pid(settings)
    status["running"] = pid_is_running(pid) if pid is not None else bool(status.get("running"))
    status["pid_file_pid"] = pid
    status["config"] = ai_gateway_safe_config(settings)
    status["log_file"] = settings.AI_GATEWAY_LOG_FILE
    payload = redact(status)
    if not isinstance(payload, dict):
        raise RuntimeError("AI gateway status redaction returned invalid payload")
    return payload


def ai_gateway_logs(settings: Settings, *, lines: int) -> list[str]:
    path = Path(settings.AI_GATEWAY_LOG_FILE)
    if not path.exists():
        return []
    entries = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    return [redact_text(line) for line in entries]


def wait_for_gateway_ready(settings: Settings, *, timeout_seconds: int = 20) -> bool:
    deadline = time.monotonic() + timeout_seconds
    openapi_url = settings.AI_GATEWAY_BASE_URL.rstrip("/") + "/openapi.json"
    httpx_logger = logging.getLogger("httpx")
    previous_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    while time.monotonic() < deadline:
        if not pid_is_running(read_pid(settings)):
            httpx_logger.setLevel(previous_level)
            return False
        try:
            response = httpx.get(openapi_url, timeout=2, trust_env=False)
            if response.status_code == 200:
                httpx_logger.setLevel(previous_level)
                return True
        except Exception:
            pass
        time.sleep(0.5)
    httpx_logger.setLevel(previous_level)
    return False


def ensure_runtime_dir(settings: Settings) -> None:
    Path(settings.AI_GATEWAY_RUNTIME_DIR).mkdir(parents=True, exist_ok=True)


def read_status(settings: Settings) -> dict[str, Any]:
    path = Path(settings.AI_GATEWAY_STATUS_FILE)
    if not path.exists():
        return {
            "running": False,
            "pid": None,
            "base_url": settings.AI_GATEWAY_BASE_URL,
            "log_file": settings.AI_GATEWAY_LOG_FILE,
            "last_error": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {
            "running": False,
            "pid": None,
            "base_url": settings.AI_GATEWAY_BASE_URL,
            "log_file": settings.AI_GATEWAY_LOG_FILE,
            "last_error": "status_parse_failed",
        }
    if not isinstance(payload, dict):
        return {
            "running": False,
            "pid": None,
            "base_url": settings.AI_GATEWAY_BASE_URL,
            "log_file": settings.AI_GATEWAY_LOG_FILE,
            "last_error": "status_parse_failed",
        }
    return payload


def write_status(settings: Settings, payload: dict[str, Any]) -> None:
    ensure_runtime_dir(settings)
    path = Path(settings.AI_GATEWAY_STATUS_FILE)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(redact(payload), indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def append_log(settings: Settings, event: str, payload: dict[str, Any]) -> None:
    ensure_runtime_dir(settings)
    line = json.dumps(
        redact(
            {
                "timestamp": time.time(),
                "event": event,
                "payload": payload,
            }
        ),
        sort_keys=True,
    )
    with Path(settings.AI_GATEWAY_LOG_FILE).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_pid(settings: Settings) -> int | None:
    path = Path(settings.AI_GATEWAY_PID_FILE)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def write_pid(settings: Settings, pid: int) -> None:
    ensure_runtime_dir(settings)
    Path(settings.AI_GATEWAY_PID_FILE).write_text(str(pid), encoding="utf-8")


def remove_pid(settings: Settings) -> None:
    path = Path(settings.AI_GATEWAY_PID_FILE)
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


def _parse_local_base_url(base_url: str) -> dict[str, Any]:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8090
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("AI gateway local runtime requires localhost/127.0.0.1 base URL")
    return {"host": "127.0.0.1" if host == "localhost" else host, "port": port}


def _build_runtime_env(settings: Settings, *, host: str, port: int) -> dict[str, str]:
    env = os.environ.copy()
    repo_root = Path.cwd().resolve()
    gateway_dir = Path(settings.AI_GATEWAY_APP_DIR).resolve()
    shims_dir = (repo_root / "src" / "triak_trade" / "ai" / "ajil_shims").resolve()
    pythonpath_parts = [str(shims_dir), str(gateway_dir)]
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["UAG_ENV_FILE"] = str((repo_root / ".env.local").resolve())
    env["UAG_APP_HOST"] = host
    env["UAG_APP_PORT"] = str(port)
    env.setdefault("UAG_REDIS_REQUIRED", "false")
    env.setdefault("UAG_POLLINATIONS_ENABLED", "false")
    env.setdefault("UAG_ADMIN_ENABLED", "false")
    if not env.get("UAG_AUTH_TOKEN"):
        env.setdefault("UAG_AUTH_ENABLED", "false")
    no_proxy_values = "127.0.0.1,localhost"
    env["NO_PROXY"] = no_proxy_values
    env["no_proxy"] = no_proxy_values
    return env
