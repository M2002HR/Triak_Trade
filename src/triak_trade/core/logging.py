"""Structured logging setup."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from datetime import datetime
from typing import Any, cast

import structlog
from pythonjsonlogger.json import JsonFormatter

from triak_trade.config.settings import Settings

SENSITIVE_KEYWORDS = ("secret", "token", "password", "api_key", "hash")
REDACTED = "***REDACTED***"


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(word in lowered for word in SENSITIVE_KEYWORDS)


def redact_value(value: Any) -> Any:
    """Recursively redact sensitive values while preserving useful structure."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, MutableMapping):
        return {
            key: (REDACTED if _is_sensitive_key(str(key)) else redact_value(item))
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, set):
        return {redact_value(item) for item in value}
    get_secret = getattr(value, "get_secret_value", None)
    if callable(get_secret):
        return REDACTED
    return value


def sanitize_log_fields(**fields: Any) -> dict[str, Any]:
    """Prepare log metadata for stdlib logging."""
    return {
        key: (REDACTED if _is_sensitive_key(key) else redact_value(value))
        for key, value in fields.items()
    }


def safe_preview(text: str | None, *, max_chars: int = 160) -> str | None:
    """Collapse whitespace and keep a short non-secret preview for logs."""
    if text is None:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return f"{collapsed[: max_chars - 3]}..."


def duration_ms(started: datetime, finished: datetime) -> int:
    """Return a non-negative duration in milliseconds."""
    return max(0, int((finished - started).total_seconds() * 1000))


def log_event(logger: logging.Logger, level: int, event: str, /, **fields: Any) -> None:
    """Emit a structured stdlib log entry."""
    logger.log(level, event, extra=sanitize_log_fields(**fields))


def bind_context(**fields: Any) -> None:
    """Bind contextual fields for structlog-compatible loggers."""
    structlog.contextvars.bind_contextvars(**sanitize_log_fields(**fields))


def clear_context() -> None:
    """Clear bound contextual fields."""
    structlog.contextvars.clear_contextvars()


class RedactSensitiveProcessor:
    """Redacts sensitive key-value pairs."""

    def __call__(
        self,
        _: structlog.types.WrappedLogger,
        __: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        for key, value in list(event_dict.items()):
            if _is_sensitive_key(key):
                event_dict[key] = REDACTED
                continue
            event_dict[key] = redact_value(value)
        return event_dict


class RenameEventKey(JsonFormatter):
    """Ensures 'event' key in stdlib JSON output."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        if "message" in log_record and "event" not in log_record:
            log_record["event"] = log_record.pop("message")


def configure_logging(settings: Settings) -> None:
    """Configure structlog + stdlib logging."""
    processor_chain: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
        RedactSensitiveProcessor(),
    ]

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.LOG_LEVEL.upper())

    handler = logging.StreamHandler(sys.stdout)
    if settings.LOG_FORMAT == "json":
        handler.setFormatter(RenameEventKey())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    root.addHandler(handler)

    structlog.configure(
        processors=[
            *processor_chain,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(module: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger for a module."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(module))
