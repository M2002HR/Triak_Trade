#!/usr/bin/env python3
"""Container service bootstrapper for Triak_Trade."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

from triak_trade.deployment.runtime_env import (
    build_ai_gateway_runtime_env,
    build_dashboard_runtime_env,
    load_root_env_file,
)

_APP_ROOT: Final[Path] = Path("/app")
_DEFAULT_ENV_FILE: Final[Path] = _APP_ROOT / ".env.local"


def _load_and_apply_root_env() -> dict[str, str]:
    env_file = Path(os.environ.get("TRIAK_ENV_FILE", str(_DEFAULT_ENV_FILE)))
    if not env_file.exists():
        raise RuntimeError(f"root env file missing: {env_file}")
    root_env = load_root_env_file(env_file)
    for key, value in root_env.items():
        os.environ[key] = value
    os.environ.setdefault(
        "PYTHONPATH",
        os.pathsep.join(
            [
                str(_APP_ROOT / "src"),
                str(_APP_ROOT / "external" / "Ajil_Unified_AI_Gateway"),
            ]
        ),
    )
    return root_env


def _wait_for_tcp(host: str, port: int, *, timeout_seconds: int = 180) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_log = 0.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError:
            now = time.monotonic()
            if now - last_log >= 15:
                remaining = int(deadline - now)
                print(f"waiting for tcp://{host}:{port} (~{remaining}s left)", flush=True)
                last_log = now
            time.sleep(1)
    raise RuntimeError(f"timed out waiting for tcp://{host}:{port}")


def _run_with_retries(
    cmd: list[str],
    *,
    cwd: str,
    attempts: int = 5,
    delay_seconds: int = 5,
) -> None:
    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(1, attempts + 1):
        try:
            subprocess.run(cmd, cwd=cwd, check=True)
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            print(
                "command failed "
                f"(attempt {attempt}/{attempts}): {' '.join(cmd)}; "
                f"retrying in {delay_seconds}s",
                flush=True,
            )
            time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def _run_dashboard() -> int:
    root_env = _load_and_apply_root_env()
    for key, value in build_dashboard_runtime_env(root_env).items():
        os.environ[key] = value
    Path("/app/runtime").mkdir(parents=True, exist_ok=True)
    Path(os.environ["TELEGRAM_SESSION_DIR"]).mkdir(parents=True, exist_ok=True)
    _wait_for_tcp("mysql", 3306)
    _wait_for_tcp("redis", 6379)
    gateway = urlparse(os.environ["AI_GATEWAY_BASE_URL"])
    gateway_host = gateway.hostname or "ai-gateway"
    gateway_port = gateway.port or 8080
    _wait_for_tcp(gateway_host, gateway_port)
    _run_with_retries(["alembic", "upgrade", "head"], cwd="/app")
    cmd = [
        "triak-trade",
        "run-dashboard",
        "--host",
        "0.0.0.0",
        "--port",
        os.environ["DASHBOARD_PORT"],
    ]
    return subprocess.call(cmd, cwd="/app")


def _run_ai_gateway() -> int:
    root_env = _load_and_apply_root_env()
    env_file = os.environ.get("TRIAK_ENV_FILE", str(_DEFAULT_ENV_FILE))
    for key, value in build_ai_gateway_runtime_env(root_env, env_file_path=env_file).items():
        os.environ[key] = value
    _wait_for_tcp("redis", 6379)
    cmd = [
        "python3",
        "-m",
        "uvicorn",
        "unified_gateway.app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
        "--log-level",
        "warning",
    ]
    return subprocess.call(cmd, cwd="/app/external/Ajil_Unified_AI_Gateway")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: run_service.py [dashboard|ai-gateway]")
    service = sys.argv[1]
    if service == "dashboard":
        return _run_dashboard()
    if service == "ai-gateway":
        return _run_ai_gateway()
    raise SystemExit(f"unknown service: {service}")


if __name__ == "__main__":
    raise SystemExit(main())
