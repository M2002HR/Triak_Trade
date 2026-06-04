from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

from triak_trade.domain.enums import CandleSource
from triak_trade.market_data.binance_public import BinancePublicFuturesProvider
from triak_trade.market_data.errors import MarketDataHTTPError


def _zip_payload(rows: list[list[object]], filename: str = "payload.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        content = "\n".join(",".join(str(item) for item in row) for row in rows)
        archive.writestr(filename, content)
    return buffer.getvalue()


def _provider(tmp_dir: str, handler: httpx.MockTransport) -> BinancePublicFuturesProvider:
    return BinancePublicFuturesProvider(
        base_url="https://data.binance.vision",
        rest_base_url="https://fapi.binance.com",
        klines_path="/fapi/v1/klines",
        ticker_price_path="/fapi/v1/ticker/price",
        cache_dir=tmp_dir,
        timeout_seconds=5,
        transport=handler,
    )


@pytest.mark.asyncio
async def test_provider_downloads_daily_archive_and_parses_candles() -> None:
    requested: list[str] = []
    start = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    row = [
        int(start.timestamp() * 1000),
        "1",
        "2",
        "0.5",
        "1.5",
        "10",
        int((start + timedelta(minutes=1)).timestamp() * 1000),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=_zip_payload([row]))

    with TemporaryDirectory() as tmp_dir:
        provider = _provider(tmp_dir, httpx.MockTransport(handler))
        candles = await provider.get_klines(
            "BTCUSDT",
            "1m",
            start,
            start + timedelta(minutes=5),
        )

    assert requested
    assert "data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-06-01.zip" in requested[0]
    assert len(candles) == 1
    assert candles[0].symbol == "BTCUSDT"
    assert candles[0].source is CandleSource.BINANCE
    assert candles[0].open == Decimal("1")


@pytest.mark.asyncio
async def test_provider_reuses_local_cache_without_second_http_call() -> None:
    calls = 0
    start = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    row = [
        int(start.timestamp() * 1000),
        "1",
        "2",
        "0.5",
        "1.5",
        "10",
        int((start + timedelta(minutes=1)).timestamp() * 1000),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_zip_payload([row]))

    with TemporaryDirectory() as tmp_dir:
        provider = _provider(tmp_dir, httpx.MockTransport(handler))
        for _ in range(2):
            candles = await provider.get_klines(
                "BTCUSDT",
                "1m",
                start,
                start + timedelta(minutes=5),
            )
            assert len(candles) == 1
        cached_files = list(Path(tmp_dir).rglob("*.zip"))

    assert calls == 1
    assert cached_files


@pytest.mark.asyncio
async def test_provider_uses_monthly_archive_for_full_past_month() -> None:
    requested: list[str] = []
    start = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
    open_time = start + timedelta(minutes=1)
    row = [
        int(open_time.timestamp() * 1000),
        "1",
        "2",
        "0.5",
        "1.5",
        "10",
        int((open_time + timedelta(minutes=1)).timestamp() * 1000),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=_zip_payload([row]))

    with TemporaryDirectory() as tmp_dir:
        provider = _provider(tmp_dir, httpx.MockTransport(handler))
        candles = await provider.get_klines(
            "BTCUSDT",
            "1m",
            start,
            datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        )

    assert candles
    assert "data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2026-04.zip" in requested[0]


@pytest.mark.asyncio
async def test_provider_get_latest_price_uses_public_rest_without_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/ticker/price"
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "103456.7"})

    with TemporaryDirectory() as tmp_dir:
        provider = _provider(tmp_dir, httpx.MockTransport(handler))
        price = await provider.get_latest_price("BTCUSDT")

    assert price == Decimal("103456.7")


@pytest.mark.asyncio
async def test_provider_handles_missing_archive_as_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    with TemporaryDirectory() as tmp_dir:
        provider = _provider(tmp_dir, httpx.MockTransport(handler))
        candles = await provider.get_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 6, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc),
        )

    assert candles == []


@pytest.mark.asyncio
async def test_provider_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with TemporaryDirectory() as tmp_dir:
        provider = _provider(tmp_dir, httpx.MockTransport(handler))
        with pytest.raises(MarketDataHTTPError):
            await provider.get_klines(
                "BTCUSDT",
                "1m",
                datetime(2026, 6, 1, tzinfo=timezone.utc),
                datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc),
            )


@pytest.mark.asyncio
async def test_provider_skips_header_row_in_archive() -> None:
    start = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    row = [
        int(start.timestamp() * 1000),
        "1",
        "2",
        "0.5",
        "1.5",
        "10",
        int((start + timedelta(minutes=1)).timestamp() * 1000),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_zip_payload([["open_time"], row]))

    with TemporaryDirectory() as tmp_dir:
        provider = _provider(tmp_dir, httpx.MockTransport(handler))
        candles = await provider.get_klines(
            "BTCUSDT",
            "1m",
            start,
            start + timedelta(minutes=2),
        )

    assert len(candles) == 1
    assert candles[0].open_time == start


@pytest.mark.asyncio
async def test_provider_uses_rest_recent_fallback_and_caches_payload() -> None:
    calls: list[str] = []
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
    row = [
        int(start.timestamp() * 1000),
        "1",
        "2",
        "0.5",
        "1.5",
        "10",
        int((start + timedelta(minutes=1)).timestamp() * 1000),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "data.binance.vision" in str(request.url):
            return httpx.Response(404, text="missing")
        return httpx.Response(200, json=[row])

    with TemporaryDirectory() as tmp_dir:
        provider = _provider(tmp_dir, httpx.MockTransport(handler))
        candles = await provider.get_klines("BTCUSDT", "1m", start, start + timedelta(minutes=2))
        cached_files = list((Path(tmp_dir) / "_rest").rglob("*.json"))

    assert candles
    assert candles[0].open_time == start
    assert any("fapi.binance.com" in call for call in calls)
    assert cached_files
