from __future__ import annotations

from pathlib import Path

from triak_trade.deployment.runtime_env import (
    build_ai_gateway_runtime_env,
    build_dashboard_runtime_env,
    load_root_env_file,
)


def test_load_root_env_file_reads_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text("A=1\nB=two\nEMPTY=\n", encoding="utf-8")
    values = load_root_env_file(env_file)
    assert values["A"] == "1"
    assert values["B"] == "two"
    assert values["EMPTY"] == ""


def test_build_dashboard_runtime_env_uses_docker_overrides() -> None:
    values = build_dashboard_runtime_env(
        {
            "DATABASE_URL_DOCKER": "mysql+pymysql://x",
            "REDIS_URL_DOCKER": "redis://redis:6379/2",
            "AI_GATEWAY_BASE_URL_DOCKER": "http://ai-gateway:8080",
            "DASHBOARD_PORT": "9090",
            "TELEGRAM_SESSION_DIR_DOCKER": "/data/sessions",
            "HTTP_PROXY": "http://host.docker.internal:2080",
        }
    )
    assert values["DATABASE_URL"] == "mysql+pymysql://x"
    assert values["REDIS_URL"] == "redis://redis:6379/2"
    assert values["AI_GATEWAY_BASE_URL"] == "http://ai-gateway:8080"
    assert values["DASHBOARD_PORT"] == "9090"
    assert values["TELEGRAM_SESSION_DIR"] == "/data/sessions"
    assert values["HTTP_PROXY"] == "http://host.docker.internal:2080"


def test_build_ai_gateway_runtime_env_maps_root_ai_settings() -> None:
    values = build_ai_gateway_runtime_env(
        {
            "UAG_GEMINI_MODE": "cloudflare_worker",
            "AI_GATEWAY_AUTH_TOKEN": "secret-token",
            "AI_GATEWAY_AUTH_HEADER_NAME": "x-api-token",
            "GEMINI_API_KEYS": "g1,g2",
            "GROQ_API_KEYS": "r1",
            "AI_CLASSIFIER_VISION_MODEL": "gemini-3.1-flash-lite",
            "UAG_REDIS_URL_DOCKER": "redis://redis:6379/5",
            "UAG_PROXY_URL_DOCKER": "http://proxy2080:3128",
            "HTTPS_PROXY": "http://host.docker.internal:2080",
        },
        env_file_path="/app/.env.local",
    )
    assert values["UAG_ENV_FILE"] == "/app/.env.local"
    assert values["UAG_AUTH_ENABLED"] == "true"
    assert values["UAG_AUTH_TOKEN"] == "secret-token"
    assert values["UAG_AUTH_HEADER_NAME"] == "x-api-token"
    assert values["UAG_GEMINI_API_KEYS"] == "g1,g2"
    assert values["UAG_GROQ_API_KEYS"] == "r1"
    assert values["UAG_GEMINI_MODE"] == "cloudflare_worker"
    assert values["UAG_GEMINI_DEFAULT_MODEL"] == "gemini-3.1-flash-lite"
    assert values["UAG_REDIS_URL"] == "redis://redis:6379/5"
    assert values["UAG_PROXY_URL"] == "http://proxy2080:3128"
    assert values["HTTPS_PROXY"] == "http://host.docker.internal:2080"


def test_build_ai_gateway_runtime_env_preserves_explicit_uag_values() -> None:
    values = build_ai_gateway_runtime_env(
        {
            "UAG_AUTH_ENABLED": "true",
            "UAG_AUTH_TOKEN": "uag-token",
            "UAG_AUTH_HEADER_NAME": "x-uag-token",
            "UAG_GEMINI_API_KEYS": "u1,u2",
            "UAG_GROQ_API_KEYS": "g1",
            "UAG_GEMINI_DEFAULT_MODEL": "gemini-3.1-flash-lite",
            "UAG_REDIS_URL": "redis://127.0.0.1:6379/9",
        },
        env_file_path="/app/.env.local",
    )
    assert values["UAG_AUTH_TOKEN"] == "uag-token"
    assert values["UAG_AUTH_HEADER_NAME"] == "x-uag-token"
    assert values["UAG_GEMINI_API_KEYS"] == "u1,u2"
    assert values["UAG_GROQ_API_KEYS"] == "g1"
    assert values["UAG_REDIS_URL"] == "redis://127.0.0.1:6379/9"
