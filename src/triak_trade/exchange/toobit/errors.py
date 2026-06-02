"""Toobit client errors."""

from __future__ import annotations

from triak_trade.exchange.errors import ExchangeError


class ToobitError(ExchangeError):
    """Base Toobit error."""


class ToobitTimeoutError(ToobitError):
    """Toobit request timed out."""


class ToobitConnectionError(ToobitError):
    """Toobit connection failed."""


class ToobitAPIError(ToobitError):
    """Toobit API status or payload error."""


class ToobitParseError(ToobitError):
    """Toobit response parse error."""
