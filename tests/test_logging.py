from __future__ import annotations

import io
import json
import logging

from triak_trade.config.settings import Settings
from triak_trade.core.logging import (
    configure_logging,
    get_logger,
    safe_preview,
    sanitize_log_fields,
)


def test_logging_emits_structured_json() -> None:
    stream = io.StringIO()
    settings = Settings(LOG_FORMAT="json")
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
    settings = Settings(LOG_FORMAT="json")
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
