from __future__ import annotations

import httpx
import pytest

from triak_trade.exchange.toobit.client import ToobitClient
from triak_trade.exchange.toobit.errors import ToobitAPIError, ToobitParseError, ToobitTimeoutError


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
async def test_signed_request_adds_headers_and_signature_params() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers.get("X-BB-APIKEY")
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    client = _client(httpx.MockTransport(handler))
    await client.signed_request("GET", "/x", params={"symbol": "BTCUSDT"})
    assert captured["header"] == "k"
    assert "timestamp" in captured["params"]
    assert captured["params"]["recvWindow"] == "5000"
    assert "signature" in captured["params"]


@pytest.mark.asyncio
async def test_public_and_error_paths() -> None:
    ok = _client(httpx.MockTransport(lambda request: httpx.Response(200, json={"serverTime": 1})))
    assert "serverTime" in await ok.get_server_time()

    api_err = _client(httpx.MockTransport(lambda request: httpx.Response(500, json={})))
    with pytest.raises(ToobitAPIError):
        await api_err.get_exchange_info()

    parse_err = _client(httpx.MockTransport(lambda request: httpx.Response(200, text="bad")))
    with pytest.raises(ToobitParseError):
        await parse_err.get_server_time()

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    timeout_client = _client(httpx.MockTransport(timeout_handler))
    with pytest.raises(ToobitTimeoutError):
        await timeout_client.get_server_time()


def test_client_repr_redacts() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    text = repr(client)
    assert "api_key=**redacted**" in text
    assert "api_secret=**redacted**" in text
