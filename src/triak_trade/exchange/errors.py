"""Exchange layer errors."""

from __future__ import annotations


class ExchangeError(Exception):
    """Base exchange error."""


class LiveTradingBlockedError(ExchangeError):
    """Raised when live trading path is requested."""


class ExchangeValidationError(ExchangeError):
    """Raised for invalid exchange request configuration."""
