from __future__ import annotations

import httpx
import pytest

from triak_trade.exchange.toobit.account import ToobitAccountClient
from triak_trade.exchange.toobit.client import ToobitClient


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
async def test_safe_account_check_skips_when_path_missing() -> None:
    account = ToobitAccountClient(
        _client(httpx.MockTransport(lambda request: httpx.Response(200, json={}))),
        "",
    )
    result = await account.safe_account_check()
    assert result.skipped is True


@pytest.mark.asyncio
async def test_safe_account_check_uses_signed_when_path_present() -> None:
    account = ToobitAccountClient(
        _client(httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
        "/safe/account",
    )
    result = await account.safe_account_check()
    assert result.success is True
    assert result.endpoint_path == "/safe/account"
