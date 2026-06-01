"""Structured logging setup."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, cast

import structlog
from pythonjsonlogger.json import JsonFormatter

from triak_trade.config.settings import Settings

SENSITIVE_KEYWORDS = ("secret", "token", "password", "api_key", "hash")


class RedactSensitiveProcessor:
    """Redacts sensitive key-value pairs."""

    def __call__(
        self,
        _: structlog.types.WrappedLogger,
        __: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        for key, value in list(event_dict.items()):
            if any(word in key.lower() for word in SENSITIVE_KEYWORDS):
                event_dict[key] = "***REDACTED***"
            elif isinstance(value, str) and any(
                word in value.lower() for word in SENSITIVE_KEYWORDS
            ):
                event_dict[key] = "***REDACTED***"
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
