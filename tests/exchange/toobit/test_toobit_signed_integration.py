from __future__ import annotations

import os

import pytest

from triak_trade.config.settings import Settings
from triak_trade.exchange.toobit.account import ToobitAccountClient
from triak_trade.exchange.toobit.client import ToobitClient


@pytest.mark.asyncio
async def test_optional_signed_integration_guarded() -> None:
    if os.getenv("RUN_TOOBIT_SIGNED_INTEGRATION_TESTS") != "1":
        pytest.skip("guard disabled")

    settings = Settings()
    client = ToobitClient(
        base_url=settings.TOOBIT_BASE_URL,
        api_key=settings.TOOBIT_API_KEY.get_secret_value(),
        api_secret=settings.TOOBIT_API_SECRET.get_secret_value(),
        timeout_seconds=settings.TOOBIT_SIGNED_TIMEOUT_SECONDS,
        recv_window=settings.TOOBIT_RECV_WINDOW,
        time_path=settings.TOOBIT_TIME_PATH,
        exchange_info_path=settings.TOOBIT_EXCHANGE_INFO_PATH,
    )
    result = await client.get_server_time()
    assert isinstance(result, dict)

    account = ToobitAccountClient(client, settings.TOOBIT_SAFE_ACCOUNT_PATH)
    safe = await account.safe_account_check()
    assert safe.skipped or safe.success


@pytest.mark.asyncio
async def test_optional_ordertest_integration_guarded() -> None:
    if os.getenv("RUN_TOOBIT_ORDERTEST_INTEGRATION_TESTS") != "1":
        pytest.skip("guard disabled")
