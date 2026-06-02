from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from triak_trade.config.settings import Settings
from triak_trade.exchange.base import ExchangeOrderRequest
from triak_trade.exchange.errors import LiveTradingBlockedError
from triak_trade.exchange.toobit.client import ToobitClient
from triak_trade.exchange.toobit.demo_execution import DemoExecutionAdapter
from triak_trade.exchange.toobit.spot import ToobitSpotClient


def _spot(settings: Settings) -> ToobitSpotClient:
    client = ToobitClient(
        base_url="https://api.toobit.com",
        api_key="k",
        api_secret="s",
        timeout_seconds=5,
        recv_window=5000,
        time_path="/api/v1/time",
        exchange_info_path="/api/v1/exchangeInfo",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    return ToobitSpotClient(client, settings)


@pytest.mark.asyncio
async def test_demo_execution_blocks_live_mode() -> None:
    settings = Settings(EXECUTION_MODE="demo")
    adapter = DemoExecutionAdapter(settings, _spot(settings))
    result = await adapter.create_demo_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.001"),
        price=Decimal("1"),
        run_order_test=False,
    )
    assert result.status == "not_submitted"

    object.__setattr__(settings, "EXECUTION_MODE", "live")
    with pytest.raises(LiveTradingBlockedError):
        await adapter.create_demo_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.001"),
            price=Decimal("1"),
            run_order_test=False,
        )


@pytest.mark.asyncio
async def test_demo_execution_validate_order_test_path() -> None:
    settings = Settings(EXECUTION_MODE="demo")
    adapter = DemoExecutionAdapter(settings, _spot(settings))
    result = await adapter.validate_order_with_order_test(
        ExchangeOrderRequest(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.001"),
            price=Decimal("1"),
        )
    )
    assert result.accepted is True
