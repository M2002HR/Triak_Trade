"""Username-based admin authorization."""

from __future__ import annotations

from triak_trade.admin_bot.errors import AdminUnauthorizedError


def normalize_username(username: str) -> str:
    value = username.strip().lower()
    if value.startswith("@"):
        value = value[1:]
    return value


class AdminAuthService:
    def __init__(self, allowed_usernames: list[str]) -> None:
        self.allowed_usernames = {
            normalize_username(item) for item in allowed_usernames if normalize_username(item)
        }

    def is_authorized_username(self, username: str | None) -> bool:
        if username is None:
            return False
        normalized = normalize_username(username)
        return bool(normalized and normalized in self.allowed_usernames)

    def require_authorized_username(self, username: str | None) -> None:
        if username is None or not username.strip():
            raise AdminUnauthorizedError("username is required for admin authorization")
        if not self.is_authorized_username(username):
            raise AdminUnauthorizedError("username is not authorized")
