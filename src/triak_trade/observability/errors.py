"""Observability-specific errors."""

from __future__ import annotations


class ObservabilityError(RuntimeError):
    """Base error for observability failures."""


class TelegramLogChannelError(ObservabilityError):
    """Telegram log channel send failed safely."""
