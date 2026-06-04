from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from triak_trade.market_data.errors import (
    MarketDataHTTPError,
    MarketDataParseError,
    MarketDataTimeoutError,
)
from triak_trade.market_data.toobit import ToobitMarketDataProvider


def _provider(handler: httpx.MockTransport) -> ToobitMarketDataProvider:
    return ToobitMarketDataProvider(
        base_url="https://api.toobit.com",
        klines_path="/quote/v1/klines",
        mark_price_klines_path="/quote/v1/markPrice/klines",
        index_klines_path="/quote/v1/index/klines",
        contract_ticker_price_path="/quote/v1/contract/ticker/price",
        timeout_seconds=5,
        limit=2,
        transport=handler,
    )


@pytest.mark.asyncio
async def test_provider_prefers_futures_contract_symbol_and_contract_params() -> None:
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured: dict[str, str] = {}
        for key, value in request.url.params.multi_items():
            captured[key] = value
        calls.append(captured)
        return httpx.Response(200, json=[])

    provider = _provider(httpx.MockTransport(handler))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    await provider.get_klines("btcusdt", "1m", start, end)

    assert calls[0]["symbol"] == "BTC-SWAP-USDT"
    assert calls[0]["interval"] == "1m"
    assert calls[0]["startTime"] == str(int(start.timestamp() * 1000))
    assert calls[0]["endTime"] == str(int(end.timestamp() * 1000))


@pytest.mark.asyncio
async def test_provider_parses_contract_payload_and_decimal_safety() -> None:
    rows = [[1700000000000, "1", "2", "0.5", "1.5", "10", 1700000060000]]
    payloads = [rows, {"data": rows}, {"result": rows}]

    for payload in payloads:
        provider = _provider(
            httpx.MockTransport(
                lambda request, payload=payload: httpx.Response(200, json=payload)
            )
        )
        candles = await provider.get_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
        )
        assert candles
        assert candles[0].symbol == "BTC-SWAP-USDT"
        assert candles[0].open == Decimal("1")
        assert isinstance(candles[0].close, Decimal)


@pytest.mark.asyncio
async def test_provider_falls_back_to_mark_price_endpoint() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, str(request.url.params.get("symbol"))))
        if request.url.path == "/quote/v1/klines":
            return httpx.Response(200, json=[])
        assert request.url.path == "/quote/v1/markPrice/klines"
        assert request.url.params["from"]
        assert request.url.params["to"]
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": [
                    {
                        "time": 1700000000000,
                        "open": "1",
                        "high": "2",
                        "low": "0.5",
                        "close": "1.5",
                        "volume": "10",
                    }
                ],
            },
        )

    provider = _provider(httpx.MockTransport(handler))
    candles = await provider.get_klines(
        "BTCUSDT",
        "1m",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
    )

    assert candles
    assert candles[0].open == Decimal("1")
    assert calls[:2] == [
        ("/quote/v1/klines", "BTC-SWAP-USDT"),
        ("/quote/v1/markPrice/klines", "BTC-SWAP-USDT"),
    ]


@pytest.mark.asyncio
async def test_provider_falls_back_to_index_endpoint() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, str(request.url.params.get("symbol"))))
        if request.url.path in {"/quote/v1/klines", "/quote/v1/markPrice/klines"}:
            return httpx.Response(200, json={"code": 200, "data": []})
        assert request.url.path == "/quote/v1/index/klines"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": [
                    {
                        "t": 1700000000000,
                        "o": "1",
                        "h": "2",
                        "l": "0.5",
                        "c": "1.5",
                        "v": "10",
                    }
                ],
            },
        )

    provider = _provider(httpx.MockTransport(handler))
    candles = await provider.get_klines(
        "BTCUSDT",
        "1m",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
    )

    assert candles
    assert candles[0].symbol == "BTCUSDT"
    assert calls[:3] == [
        ("/quote/v1/klines", "BTC-SWAP-USDT"),
        ("/quote/v1/markPrice/klines", "BTC-SWAP-USDT"),
        ("/quote/v1/index/klines", "BTCUSDT"),
    ]


@pytest.mark.asyncio
async def test_get_latest_price_uses_contract_ticker_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/quote/v1/contract/ticker/price"
        assert request.url.params["symbol"] == "BTC-SWAP-USDT"
        return httpx.Response(200, json=[{"s": "BTC-SWAP-USDT", "p": "104321.50"}])

    provider = _provider(httpx.MockTransport(handler))
    price = await provider.get_latest_price("BTCUSDT")

    assert price == Decimal("104321.50")


@pytest.mark.asyncio
async def test_empty_response_non_2xx_timeout_malformed_invalid_row() -> None:
    provider = _provider(httpx.MockTransport(lambda request: httpx.Response(200, json=[])))
    candles = await provider.get_klines(
        "BTCUSDT",
        "1m",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
    )
    assert candles == []

    provider_http = _provider(httpx.MockTransport(lambda request: httpx.Response(500, json={})))
    with pytest.raises(MarketDataHTTPError):
        await provider_http.get_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        )

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("boom")

    provider_timeout = _provider(httpx.MockTransport(timeout_handler))
    with pytest.raises(MarketDataTimeoutError):
        await provider_timeout.get_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        )

    provider_bad_json = _provider(
        httpx.MockTransport(lambda request: httpx.Response(200, text="not-json"))
    )
    with pytest.raises(MarketDataParseError):
        await provider_bad_json.get_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        )

    provider_bad_row = _provider(
        httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"x": 1}]}))
    )
    with pytest.raises(MarketDataParseError):
        await provider_bad_row.get_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_chunking_dedup_and_sorted() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        start = int(request.url.params["startTime"])
        row = [start, "1", "2", "0.5", "1.5", "10", start + 60000]
        overlap = [start, "1", "2", "0.5", "1.5", "10", start + 60000]
        return httpx.Response(200, json=[row, overlap])

    provider = _provider(httpx.MockTransport(handler))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    candles = await provider.get_klines("BTCUSDT", "1m", start, end)

    assert calls >= 2
    assert candles == sorted(candles, key=lambda item: item.open_time)
    assert len(candles) == len({c.open_time for c in candles})
