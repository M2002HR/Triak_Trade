from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from triak_trade.config.settings import Settings
from triak_trade.market_data.binance_public import BinancePublicFuturesProvider
from triak_trade.market_data.errors import MarketDataError


@pytest.mark.asyncio
async def test_optional_binance_public_historical_integration() -> None:
    if os.getenv("RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS") != "1":
        pytest.skip("guard disabled")

    settings = Settings()
    provider = BinancePublicFuturesProvider(
        base_url=settings.BINANCE_PUBLIC_DATA_BASE_URL,
        rest_base_url=settings.BINANCE_FUTURES_REST_BASE_URL,
        klines_path=settings.BINANCE_FUTURES_KLINES_PATH,
        ticker_price_path=settings.BINANCE_FUTURES_TICKER_PRICE_PATH,
        cache_dir=settings.BINANCE_PUBLIC_DATA_CACHE_DIR,
        timeout_seconds=settings.BINANCE_PUBLIC_DATA_TIMEOUT_SECONDS,
    )
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=5)
    try:
        candles = await provider.get_klines(
            settings.BINANCE_PUBLIC_REAL_TEST_SYMBOL,
            settings.TOOBIT_MARKET_DATA_DEFAULT_INTERVAL,
            start,
            end,
        )
    except MarketDataError as exc:
        assert "binance" in str(exc).lower() or "market data" in str(exc).lower()
    else:
        assert isinstance(candles, list)
