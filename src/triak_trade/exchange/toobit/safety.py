"""Toobit safety guards."""

from __future__ import annotations

from decimal import Decimal

from triak_trade.config.settings import Settings
from triak_trade.exchange.errors import ExchangeValidationError, LiveTradingBlockedError


def ensure_demo_mode(settings: Settings) -> None:
    mode = str(settings.EXECUTION_MODE)
    if mode == "live":
        raise LiveTradingBlockedError("Live mode is blocked")
    if mode != "demo":
        raise ExchangeValidationError("Order test requires EXECUTION_MODE=demo")


def require_guard(enabled: bool, message: str) -> None:
    if not enabled:
        raise ExchangeValidationError(message)


def ensure_explicit_order_test_params(
    *,
    symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal | None,
    price: Decimal | None,
) -> None:
    if not symbol.strip() or not side.strip() or not order_type.strip():
        raise ExchangeValidationError("symbol/side/type are required")
    if quantity is None:
        raise ExchangeValidationError("quantity is required")
    if quantity <= Decimal("0"):
        raise ExchangeValidationError("quantity must be positive")
    if order_type.strip().upper() == "LIMIT":
        if price is None:
            raise ExchangeValidationError("price is required for LIMIT")
        if price <= Decimal("0"):
            raise ExchangeValidationError("price must be positive")
