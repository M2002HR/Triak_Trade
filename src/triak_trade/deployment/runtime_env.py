"""Container runtime environment mapping helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

from dotenv import dotenv_values

_DEFAULT_DATABASE_URL_DOCKER = (
    "mysql+pymysql://triak:triak_local_password@mysql:3306/triak_trade?charset=utf8mb4"
)
_DEFAULT_REDIS_URL_DOCKER = "redis://redis:6379/0"
_DEFAULT_AI_GATEWAY_BASE_URL_DOCKER = "http://ai-gateway:8080"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_TELEGRAM_PROXY_ENV_KEYS = (
    "TELEGRAM_PROXY_ENABLED",
    "TELEGRAM_PROXY_TYPE",
    "TELEGRAM_PROXY_HOST",
    "TELEGRAM_PROXY_PORT",
    "TELEGRAM_PROXY_RDNS",
    "TELEGRAM_PROXY_USERNAME",
    "TELEGRAM_PROXY_PASSWORD",
)


def _copy_present(root_env: Mapping[str, str], keys: tuple[str, ...]) -> dict[str, str]:
    return {
        key: value
        for key in keys
        if (value := root_env.get(key, "")).strip()
    }


def load_root_env_file(path: str | Path) -> dict[str, str]:
    values = dotenv_values(Path(path))
    return {
        str(key): str(value)
        for key, value in values.items()
        if key is not None and value is not None
    }


def _dockerize_loopback_host(host: str) -> str:
    if host.strip().lower() in _LOOPBACK_HOSTS:
        return "host.docker.internal"
    return host


def _dockerize_proxy_url(value: str) -> str:
    if not value.strip():
        return value
    parsed = urlsplit(value)
    if not parsed.scheme or parsed.hostname is None:
        return value
    if parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        return value

    auth = ""
    if parsed.username is not None:
        auth = parsed.username
        if parsed.password is not None:
            auth = f"{auth}:{parsed.password}"
        auth = f"{auth}@"
    port = f":{parsed.port}" if parsed.port is not None else ""
    updated = SplitResult(
        scheme=parsed.scheme,
        netloc=f"{auth}host.docker.internal{port}",
        path=parsed.path,
        query=parsed.query,
        fragment=parsed.fragment,
    )
    return urlunsplit(updated)


def _telegram_http_proxy_candidates(root_env: Mapping[str, str]) -> list[str]:
    if root_env.get("TELEGRAM_PROXY_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return []
    proxy_type = root_env.get("TELEGRAM_PROXY_TYPE", "").strip().lower()
    if proxy_type not in {"http", "https", "http_connect"}:
        return []

    candidates: list[str] = []
    for host_key, port_key in (
        ("TELEGRAM_PROXY_HOST_DOCKER", "TELEGRAM_PROXY_PORT_DOCKER"),
        ("TELEGRAM_PROXY_HOST", "TELEGRAM_PROXY_PORT"),
    ):
        host = root_env.get(host_key, "").strip()
        port = root_env.get(port_key, "").strip()
        if not host or not port:
            continue
        candidates.append(f"http://{_dockerize_loopback_host(host)}:{port}")
    return candidates


def build_container_proxy_candidates(
    root_env: Mapping[str, str],
    *,
    include_uag: bool,
) -> list[str]:
    raw_candidates: list[str] = []
    if include_uag:
        raw_candidates.extend(
            root_env.get(key, "").strip()
            for key in ("UAG_PROXY_URL_DOCKER", "UAG_PROXY_URL")
        )
    raw_candidates.extend(
        root_env.get(key, "").strip()
        for key in (
            "HTTP_PROXY_DOCKER",
            "HTTPS_PROXY_DOCKER",
            "ALL_PROXY_DOCKER",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
        )
    )
    raw_candidates.extend(_telegram_http_proxy_candidates(root_env))

    candidates: list[str] = []
    seen: set[str] = set()
    for value in raw_candidates:
        normalized = _dockerize_proxy_url(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
    return candidates


def choose_first_reachable_proxy_url(
    candidates: list[str],
    *,
    probe: Callable[[str], bool],
) -> str | None:
    for candidate in candidates:
        if probe(candidate):
            return candidate
    return None


def _copy_proxy_env(root_env: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in _PROXY_ENV_KEYS:
        docker_key = f"{key}_DOCKER"
        docker_value = root_env.get(docker_key, "").strip()
        base_value = root_env.get(key, "").strip()
        value = docker_value or _dockerize_proxy_url(base_value)
        if value:
            values[key] = value
    return values


def _copy_telegram_proxy_env(root_env: Mapping[str, str]) -> dict[str, str]:
    values = _copy_present(root_env, _TELEGRAM_PROXY_ENV_KEYS)
    host = values.get("TELEGRAM_PROXY_HOST", "").strip()
    if host:
        values["TELEGRAM_PROXY_HOST"] = _dockerize_loopback_host(host)
    return values


def build_dashboard_runtime_env(root_env: Mapping[str, str]) -> dict[str, str]:
    env = {
        "DATABASE_URL": root_env.get("DATABASE_URL_DOCKER", _DEFAULT_DATABASE_URL_DOCKER),
        "REDIS_URL": root_env.get("REDIS_URL_DOCKER", _DEFAULT_REDIS_URL_DOCKER),
        "AI_GATEWAY_BASE_URL": root_env.get(
            "AI_GATEWAY_BASE_URL_DOCKER",
            _DEFAULT_AI_GATEWAY_BASE_URL_DOCKER,
        ),
        "DASHBOARD_HOST": "0.0.0.0",
        "DASHBOARD_PORT": root_env.get("DASHBOARD_PORT", "8088"),
        "TELEGRAM_SESSION_DIR": root_env.get("TELEGRAM_SESSION_DIR_DOCKER", "/app/.sessions"),
    }
    env.update(_copy_proxy_env(root_env))
    env.update(_copy_telegram_proxy_env(root_env))
    return env


def build_ai_gateway_runtime_env(
    root_env: Mapping[str, str],
    *,
    env_file_path: str,
) -> dict[str, str]:
    auth_token = root_env.get("UAG_AUTH_TOKEN") or root_env.get("AI_GATEWAY_AUTH_TOKEN", "")
    env = {
        key: value
        for key, value in root_env.items()
        if key.startswith("UAG_") and value.strip()
    }
    env.update({
        "UAG_ENV_FILE": env_file_path,
        "UAG_APP_HOST": "0.0.0.0",
        "UAG_APP_PORT": root_env.get("UAG_APP_PORT", "8080"),
        "UAG_REDIS_REQUIRED": root_env.get(
            "UAG_REDIS_REQUIRED",
            env.get("UAG_REDIS_REQUIRED", "false"),
        ),
        "UAG_REDIS_URL": root_env.get(
            "UAG_REDIS_URL_DOCKER",
            env.get("UAG_REDIS_URL", _DEFAULT_REDIS_URL_DOCKER),
        ),
        "UAG_PROXY_URL": root_env.get(
            "UAG_PROXY_URL_DOCKER",
            env.get("UAG_PROXY_URL", ""),
        ),
        "UAG_AUTH_ENABLED": root_env.get(
            "UAG_AUTH_ENABLED",
            "true" if auth_token.strip() else "false",
        ),
        "UAG_AUTH_TOKEN": auth_token,
        "UAG_AUTH_HEADER_NAME": root_env.get(
            "UAG_AUTH_HEADER_NAME",
            root_env.get("AI_GATEWAY_AUTH_HEADER_NAME", "x-api-token"),
        ),
        "UAG_ADMIN_ENABLED": root_env.get("UAG_ADMIN_ENABLED", "false"),
        "UAG_GEMINI_API_KEYS": root_env.get(
            "UAG_GEMINI_API_KEYS",
            root_env.get("GEMINI_API_KEYS", ""),
        ),
        "UAG_GROQ_API_KEYS": root_env.get(
            "UAG_GROQ_API_KEYS",
            root_env.get("GROQ_API_KEYS", ""),
        ),
        "UAG_GEMINI_DEFAULT_MODEL": root_env.get(
            "UAG_GEMINI_DEFAULT_MODEL",
            root_env.get("AI_CLASSIFIER_VISION_MODEL", "gemini-3.1-flash-lite"),
        ),
        "UAG_GEMINI_API_VERSION": root_env.get("UAG_GEMINI_API_VERSION", "v1beta"),
        "UAG_POLLINATIONS_ENABLED": root_env.get("UAG_POLLINATIONS_ENABLED", "false"),
    })
    env.update(_copy_proxy_env(root_env))
    return env
