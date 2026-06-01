"""Market data specific errors."""

from __future__ import annotations


class MarketDataError(Exception):
    """Base market data error."""


class MarketDataTimeoutError(MarketDataError):
    """Provider request timed out."""


class MarketDataConnectionError(MarketDataError):
    """Provider connection failed."""


class MarketDataHTTPError(MarketDataError):
    """Provider returned non-success status."""


class MarketDataParseError(MarketDataError):
    """Provider payload parse failed."""
