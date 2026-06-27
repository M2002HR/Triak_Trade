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

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: int | str | None = None,
        payload: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.payload = payload


class ToobitParseError(ToobitError):
    """Toobit response parse error."""
