"""Admin bot errors."""

from __future__ import annotations


class AdminBotError(Exception):
    """Base admin bot error."""


class AdminUnauthorizedError(AdminBotError):
    """Raised when admin user is not authorized."""


class AdminCallbackParseError(AdminBotError):
    """Raised on malformed callback data."""


class AdminRegistrationError(AdminBotError):
    """Raised when admin registration/chat is missing."""
