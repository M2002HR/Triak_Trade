"""Toobit spot order-test wrapper."""

from __future__ import annotations

from decimal import Decimal

from triak_trade.config.settings import Settings
from triak_trade.exchange.base import ExchangeOrderRequest, ExchangeOrderTestResult
from triak_trade.exchange.toobit.client import ToobitClient
from triak_trade.exchange.toobit.safety import ensure_demo_mode, ensure_explicit_order_test_params


class ToobitSpotClient:
    def __init__(self, client: ToobitClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def test_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
    ) -> ExchangeOrderTestResult:
        ensure_demo_mode(self.settings)
        ensure_explicit_order_test_params(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
        )
        request = ExchangeOrderRequest(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
        )
        params: dict[str, object] = {
            "symbol": request.symbol,
            "side": request.side,
            "type": request.order_type,
            "quantity": str(request.quantity),
        }
        if request.order_type == "LIMIT":
            params["price"] = str(request.price)
            params["timeInForce"] = "GTC"

        await self.client.signed_request(
            "POST",
            self.settings.TOOBIT_SPOT_ORDER_TEST_PATH,
            params=params,
        )
        return ExchangeOrderTestResult(
            accepted=True,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            status="validated",
        )
