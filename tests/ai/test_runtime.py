from __future__ import annotations

from pathlib import Path

from triak_trade.ai.runtime import (
    ai_gateway_safe_config,
    ai_gateway_status,
    stop_ai_gateway_process,
)
from triak_trade.config.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    runtime_dir = tmp_path / 'ai_gateway'
    return Settings(
        _env_file=None,
        AI_GATEWAY_ENABLED=True,
        AI_GATEWAY_BASE_URL='http://127.0.0.1:8090',
        AI_GATEWAY_RUNTIME_DIR=str(runtime_dir),
        AI_GATEWAY_PID_FILE=str(runtime_dir / 'gateway.pid'),
        AI_GATEWAY_STATUS_FILE=str(runtime_dir / 'status.json'),
        AI_GATEWAY_LOG_FILE=str(runtime_dir / 'gateway.log'),
    )


def test_ai_gateway_safe_config_redacts_secrets(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    payload = ai_gateway_safe_config(settings)
    assert payload['enabled'] is True
    assert payload['auth_token_present'] is False
    assert payload['port'] == 8090


def test_ai_gateway_status_defaults_when_not_running(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    payload = ai_gateway_status(settings)
    assert payload['running'] is False
    assert payload['pid_file_pid'] is None


def test_ai_gateway_stop_without_process_is_safe(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    result = stop_ai_gateway_process(settings)
    assert result['stopped'] is True
    assert result['running_before'] is False
