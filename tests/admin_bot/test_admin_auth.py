from __future__ import annotations

import pytest

from triak_trade.admin_bot.auth import AdminAuthService
from triak_trade.admin_bot.errors import AdminUnauthorizedError


def test_admin_auth_parsing_and_case_insensitive() -> None:
    auth = AdminAuthService(["@we_are_waiting_for_him"])
    assert auth.is_authorized_username("@we_are_waiting_for_him")
    assert auth.is_authorized_username("we_are_waiting_for_him")
    assert auth.is_authorized_username("@WE_ARE_WAITING_FOR_HIM")
    assert not auth.is_authorized_username("@not_allowed")


def test_admin_auth_require_rejects_missing_and_unauthorized() -> None:
    auth = AdminAuthService(["@we_are_waiting_for_him"])
    with pytest.raises(AdminUnauthorizedError):
        auth.require_authorized_username(None)
    with pytest.raises(AdminUnauthorizedError):
        auth.require_authorized_username("@not_allowed")
