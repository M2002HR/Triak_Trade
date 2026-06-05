#!/usr/bin/env python3
"""Ajil gateway bootstrap wrapper for Docker Compose."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "")
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    triak_src = Path("/triak_src")
    if triak_src.exists():
        sys.path.insert(0, str(triak_src))

    from triak_trade.deployment.ajil_bootstrap import (
        pollinations_module_exists,
        prepare_optional_provider_stubs,
    )

    gateway_root = Path("/app")
    stub_root = gateway_root
    pollinations_enabled = _bool_env("UAG_POLLINATIONS_ENABLED", default=False)
    if pollinations_enabled and not pollinations_module_exists(gateway_root):
        print(
            "triak-ajil-bootstrap: disabling pollinations because the provider module is absent",
            file=sys.stderr,
            flush=True,
        )
        os.environ["UAG_POLLINATIONS_ENABLED"] = "false"
        pollinations_enabled = False
    extra_paths = prepare_optional_provider_stubs(
        gateway_root=gateway_root,
        stub_root=stub_root,
        pollinations_enabled=pollinations_enabled,
    )
    existing_pythonpath = os.environ.get("PYTHONPATH", "").strip()
    path_parts = [*extra_paths]
    if existing_pythonpath:
        path_parts.append(existing_pythonpath)
    if path_parts:
        os.environ["PYTHONPATH"] = os.pathsep.join(path_parts)

    command = [
        "uvicorn",
        "unified_gateway.app.main:app",
        "--host",
        os.environ.get("UAG_APP_HOST", "0.0.0.0"),
        "--port",
        os.environ.get("UAG_APP_PORT", "8080"),
        "--log-level",
        os.environ.get("UAG_APP_LOG_LEVEL", "warning").lower(),
    ]
    return subprocess.call(command, cwd="/app")


if __name__ == "__main__":
    raise SystemExit(main())
