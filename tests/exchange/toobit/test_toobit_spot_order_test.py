from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from triak_trade.config.settings import Settings
from triak_trade.exchange.errors import ExchangeValidationError
from triak_trade.exchange.toobit.client import ToobitClient
from triak_trade.exchange.toobit.spot import ToobitSpotClient


def _client(handler: httpx.MockTransport) -> ToobitClient:
    return ToobitClient(
        base_url="https://api.toobit.com",
        api_key="k",
        api_secret="s",
        timeout_seconds=5,
        recv_window=5000,
        time_path="/api/v1/time",
        exchange_info_path="/api/v1/exchangeInfo",
        transport=handler,
    )


@pytest.mark.asyncio
async def test_spot_order_test_builds_signed_request_and_never_live_order_path() -> None:
    captured_path = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_path
        captured_path = request.url.path
        return httpx.Response(200, json={})

    settings = Settings(EXECUTION_MODE="demo", TOOBIT_SPOT_ORDER_TEST_PATH="/api/v1/spot/orderTest")
    spot = ToobitSpotClient(_client(httpx.MockTransport(handler)), settings)
    result = await spot.test_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.001"),
        price=Decimal("1"),
    )
    assert result.accepted is True
    assert captured_path == "/api/v1/spot/orderTest"
    assert captured_path != "/api/v1/spot/order"


@pytest.mark.asyncio
async def test_spot_order_test_requires_quantity_and_price_for_limit() -> None:
    settings = Settings(EXECUTION_MODE="demo")
    spot = ToobitSpotClient(
        _client(httpx.MockTransport(lambda request: httpx.Response(200, json={}))),
        settings,
    )
    with pytest.raises(ExchangeValidationError):
        await spot.test_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0"),
            price=Decimal("1"),
        )

    with pytest.raises(ExchangeValidationError):
        await spot.test_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.001"),
            price=None,
        )
