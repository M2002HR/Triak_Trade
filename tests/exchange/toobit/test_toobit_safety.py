from __future__ import annotations

from decimal import Decimal

import pytest

from triak_trade.config.settings import Settings
from triak_trade.exchange.errors import ExchangeValidationError, LiveTradingBlockedError
from triak_trade.exchange.toobit.safety import ensure_demo_mode, ensure_explicit_order_test_params


def test_safety_demo_mode_and_params() -> None:
    ensure_demo_mode(Settings(EXECUTION_MODE="demo"))

    with pytest.raises(ExchangeValidationError):
        ensure_demo_mode(Settings(EXECUTION_MODE="paper"))

    settings = Settings(EXECUTION_MODE="demo")
    object.__setattr__(settings, "EXECUTION_MODE", "live")
    with pytest.raises(LiveTradingBlockedError):
        ensure_demo_mode(settings)

    ensure_explicit_order_test_params(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.001"),
        price=Decimal("1"),
    )

    with pytest.raises(ExchangeValidationError):
        ensure_explicit_order_test_params(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=None,
            price=Decimal("1"),
        )
