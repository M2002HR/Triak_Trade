from __future__ import annotations

from decimal import Decimal

from triak_trade.exchange.base import ExchangeOrderRequest


def test_exchange_order_request_normalizes_text() -> None:
    request = ExchangeOrderRequest(
        symbol=" btcusdt ",
        side=" buy ",
        order_type=" limit ",
        quantity=Decimal("0.1"),
        price=Decimal("1"),
    )
    assert request.symbol == "BTCUSDT"
    assert request.side == "BUY"
    assert request.order_type == "LIMIT"
