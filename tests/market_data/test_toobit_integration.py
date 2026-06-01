from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from triak_trade.config.settings import Settings
from triak_trade.market_data.errors import MarketDataError
from triak_trade.market_data.toobit import ToobitMarketDataProvider


@pytest.mark.asyncio
async def test_optional_toobit_public_klines_integration() -> None:
    if os.getenv("RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS") != "1":
        pytest.skip("guard disabled")

    settings = Settings()
    provider = ToobitMarketDataProvider(
        base_url=settings.TOOBIT_BASE_URL,
        klines_path=settings.TOOBIT_KLINES_PATH,
        timeout_seconds=settings.TOOBIT_MARKET_DATA_TIMEOUT_SECONDS,
        limit=settings.TOOBIT_MARKET_DATA_LIMIT,
    )
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=5)
    try:
        candles = await provider.get_klines(
            settings.TOOBIT_REAL_TEST_SYMBOL,
            settings.TOOBIT_MARKET_DATA_DEFAULT_INTERVAL,
            start,
            end,
        )
    except MarketDataError as exc:
        assert "toobit" in str(exc).lower() or "market data" in str(exc).lower()
    else:
        assert isinstance(candles, list)
