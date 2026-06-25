"""Container runtime environment mapping helpers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

_DEFAULT_DATABASE_URL_DOCKER = (
    "mysql+pymysql://triak:triak_local_password@mysql:3306/triak_trade?charset=utf8mb4"
)
_DEFAULT_REDIS_URL_DOCKER = "redis://redis:6379/0"
_DEFAULT_AI_GATEWAY_BASE_URL_DOCKER = "http://ai-gateway:8080"
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


def _copy_proxy_env(root_env: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in _PROXY_ENV_KEYS:
        docker_key = f"{key}_DOCKER"
        value = root_env.get(docker_key, "").strip() or root_env.get(key, "").strip()
        if value:
            values[key] = value
    return values


def _copy_telegram_proxy_env(root_env: Mapping[str, str]) -> dict[str, str]:
    values = _copy_present(root_env, _TELEGRAM_PROXY_ENV_KEYS)
    for key in _TELEGRAM_PROXY_ENV_KEYS:
        docker_key = f"{key}_DOCKER"
        docker_value = root_env.get(docker_key, "").strip()
        if docker_value:
            values[key] = docker_value
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
