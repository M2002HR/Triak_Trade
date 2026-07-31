from __future__ import annotations

import io
import json
import logging

from triak_trade.config.settings import Settings
from triak_trade.core.logging import (
    configure_logging,
    get_logger,
    log_event,
    safe_preview,
    sanitize_log_fields,
)


def test_logging_emits_structured_json() -> None:
    stream = io.StringIO()
    settings = Settings(_env_file=None, LOG_FORMAT="json", DASHBOARD_FILE_LOG_ENABLED=False)
    configure_logging(settings)

    root = logging.getLogger()
    assert root.handlers
    root.handlers[0].stream = stream

    logger = get_logger(__name__)
    logger.info("signal_received", module="test", correlation_id="cid-1", action_id="a-1")

    payload = json.loads(stream.getvalue().strip())
    assert payload["event"] == "signal_received"
    assert payload["module"] == "test"
    assert payload["correlation_id"] == "cid-1"
    assert payload["action_id"] == "a-1"


def test_logging_redacts_secret_values() -> None:
    stream = io.StringIO()
    settings = Settings(_env_file=None, LOG_FORMAT="json", DASHBOARD_FILE_LOG_ENABLED=False)
    configure_logging(settings)

    root = logging.getLogger()
    root.handlers[0].stream = stream

    logger = get_logger(__name__)
    logger.info("auth", fake_secret="top-secret", api_key="abc")

    payload = json.loads(stream.getvalue().strip())
    assert payload["fake_secret"] == "***REDACTED***"
    assert payload["api_key"] == "***REDACTED***"


def test_sanitize_log_fields_redacts_nested_sensitive_values() -> None:
    payload = sanitize_log_fields(
        auth={"token": "abc", "enabled": True},
        api_secret="x",
        nested=[{"password": "hidden"}],
    )

    assert payload["auth"]["token"] == "***REDACTED***"
    assert payload["auth"]["enabled"] is True
    assert payload["api_secret"] == "***REDACTED***"
    assert payload["nested"][0]["password"] == "***REDACTED***"


def test_safe_preview_collapses_whitespace_and_truncates() -> None:
    preview = safe_preview("hello   world\nthis is a long line", max_chars=14)
    assert preview == "hello world..."


def test_file_logging_captures_debug_fields_and_redacts(tmp_path) -> None:
    log_path = tmp_path / "dashboard.log"
    settings = Settings(
        _env_file=None,
        LOG_FORMAT="human",
        LOG_LEVEL="INFO",
        DASHBOARD_FILE_LOG_ENABLED=True,
        DASHBOARD_LOG_LEVEL="DEBUG",
        DASHBOARD_LOG_FILE=str(log_path),
        DASHBOARD_LOG_MAX_BYTES=1024,
        DASHBOARD_LOG_BACKUP_COUNT=2,
    )
    configure_logging(settings)

    logger = logging.getLogger("triak_trade.test")
    log_event(
        logger,
        logging.DEBUG,
        "detail_captured",
        message_id=42,
        api_key="must-not-appear",
        detail="Authorization: bearer-value",
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    payload = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert payload["event"] == "detail_captured"
    assert payload["level"] == "DEBUG"
    assert payload["module"] == "triak_trade.test"
    assert payload["message_id"] == 42
    assert payload["api_key"] == "***REDACTED***"
    assert "bearer-value" not in log_path.read_text(encoding="utf-8")
