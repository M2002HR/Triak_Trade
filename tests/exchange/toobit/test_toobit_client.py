from __future__ import annotations

import logging

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
async def test_request_logging_sanitizes_auth_and_signature(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client(httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG):
        await client.signed_request("GET", "/x", params={"symbol": "BTCUSDT"})

    started = next(rec for rec in caplog.records if rec.msg == "toobit.request_started")
    completed = next(rec for rec in caplog.records if rec.msg == "toobit.request_completed")
    assert started.has_auth_header is True
    assert "signature" not in (started.params or {})
    assert started.path == "/x"
    assert completed.status_code == 200
    assert completed.payload_type == "dict"
    triak_records = [
        rec for rec in caplog.records if rec.name == "triak_trade.exchange.toobit.client"
    ]
    for rec in triak_records:
        assert "X-BB-APIKEY" not in rec.getMessage()
        assert "signature" not in rec.getMessage()
        assert "api_secret" not in rec.getMessage()


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


@pytest.mark.asyncio
async def test_signed_request_resyncs_time_on_recv_window_error() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/api/v1/time":
            return httpx.Response(200, json={"serverTime": 1700000005000})
        if len(calls) == 1:
            return httpx.Response(
                400,
                json={
                    "code": -1021,
                    "msg": "Timestamp for this request is outside of the recvWindow.",
                },
            )
        return httpx.Response(200, json={"ok": True})

    client = _client(httpx.MockTransport(handler))
    result = await client.signed_request("GET", "/x", params={"symbol": "BTCUSDT"})
    assert result == {"ok": True}
    assert any("/api/v1/time" in call for call in calls)


@pytest.mark.asyncio
async def test_signed_request_logs_timestamp_resync(caplog) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/api/v1/time":
            return httpx.Response(200, json={"serverTime": 1700000005000})
        if len(calls) == 1:
            return httpx.Response(
                400,
                json={
                    "code": -1021,
                    "msg": "Timestamp for this request is outside of the recvWindow.",
                },
            )
        return httpx.Response(200, json={"ok": True})

    client = _client(httpx.MockTransport(handler))
    with caplog.at_level(logging.INFO):
        await client.signed_request("GET", "/x", params={"symbol": "BTCUSDT"})

    resync = next(rec for rec in caplog.records if rec.msg == "toobit.timestamp_resync_requested")
    synced = next(rec for rec in caplog.records if rec.msg == "toobit.server_time_offset_synced")
    assert resync.error_code == -1021
    assert synced.new_offset_ms is not None


@pytest.mark.asyncio
async def test_http_error_preserves_status_code_and_payload() -> None:
    client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(400, json={"code": -1, "msg": "bad request"})
        )
    )
    with pytest.raises(ToobitAPIError) as exc_info:
        await client.signed_request("GET", "/x", params={"symbol": "BTCUSDT"})
    assert exc_info.value.status_code == 400
    assert exc_info.value.error_code == -1
    assert exc_info.value.payload == {"code": -1, "msg": "bad request"}


def test_client_repr_redacts() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    text = repr(client)
    assert "api_key=**redacted**" in text
    assert "api_secret=**redacted**" in text
