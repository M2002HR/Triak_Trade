from __future__ import annotations

import pytest

from triak_trade.admin_bot.callbacks import is_supported_menu_callback, parse_admin_callback
from triak_trade.admin_bot.errors import AdminCallbackParseError
from triak_trade.domain.enums import AdminDecisionType


def test_callback_parser_valid_cases() -> None:
    assert parse_admin_callback("admin:approve:abc").decision is AdminDecisionType.APPROVE
    assert parse_admin_callback("admin:reject:abc").decision is AdminDecisionType.REJECT
    assert parse_admin_callback("admin:watch:abc").decision is AdminDecisionType.WATCH_ONLY


def test_callback_parser_invalid_cases() -> None:
    with pytest.raises(AdminCallbackParseError):
        parse_admin_callback("bad")
    with pytest.raises(AdminCallbackParseError):
        parse_admin_callback("admin:unknown:abc")


def test_menu_callback_namespaces() -> None:
    assert is_supported_menu_callback("menu:backtest")
    assert is_supported_menu_callback("backtest:run")
    assert is_supported_menu_callback("backtest:real:24h")
    assert is_supported_menu_callback("backtest:latest")
    assert not is_supported_menu_callback("backtest:unknown")
