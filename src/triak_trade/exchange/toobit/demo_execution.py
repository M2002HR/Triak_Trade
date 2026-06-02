"""Demo execution adapter skeleton."""

from __future__ import annotations

from decimal import Decimal

from triak_trade.config.settings import Settings
from triak_trade.exchange.base import ExchangeOrderRequest, ExchangeOrderTestResult
from triak_trade.exchange.errors import LiveTradingBlockedError
from triak_trade.exchange.toobit.spot import ToobitSpotClient


class DemoExecutionAdapter:
    def __init__(self, settings: Settings, spot_client: ToobitSpotClient) -> None:
        self.settings = settings
        self.spot_client = spot_client

    async def validate_order_with_order_test(
        self,
        request: ExchangeOrderRequest,
    ) -> ExchangeOrderTestResult:
        return await self.spot_client.test_order(
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
        )

    async def create_demo_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        run_order_test: bool = False,
    ) -> ExchangeOrderTestResult:
        mode = str(self.settings.EXECUTION_MODE)
        if mode == "live":
            raise LiveTradingBlockedError("Live mode is blocked")

        if run_order_test:
            return await self.validate_order_with_order_test(
                ExchangeOrderRequest(
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=quantity,
                    price=price,
                )
            )

        return ExchangeOrderTestResult(
            accepted=False,
            symbol=symbol.strip().upper(),
            side=side.strip().upper(),
            order_type=order_type.strip().upper(),
            status="not_submitted",
            detail="validation only mode",
        )

    async def cancel_demo_order(self, order_id: str) -> dict[str, str]:
        if str(self.settings.EXECUTION_MODE) == "live":
            raise LiveTradingBlockedError("Live mode is blocked")
        return {"status": "not_submitted", "order_id": order_id}
