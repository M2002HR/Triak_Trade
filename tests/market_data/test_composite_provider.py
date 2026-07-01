from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from triak_trade.domain.enums import CandleSource
from triak_trade.domain.models import Candle
from triak_trade.market_data.composite import CompositeMarketDataProvider
from triak_trade.market_data.errors import MarketDataConnectionError


class _FailingProvider:
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        raise MarketDataConnectionError("fail")

    async def get_latest_price(self, symbol: str) -> Decimal:
        raise MarketDataConnectionError("fail")


class _WorkingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        self.calls += 1
        return [
            Candle(
                symbol=symbol,
                interval=interval,
                open_time=start_time,
                close_time=start_time + timedelta(minutes=1),
                open=Decimal("1"),
                high=Decimal("2"),
                low=Decimal("0.5"),
                close=Decimal("1.5"),
                volume=Decimal("10"),
                source=CandleSource.BINANCE,
            )
        ]

    async def get_latest_price(self, symbol: str) -> Decimal:
        return Decimal("123")


@pytest.mark.asyncio
async def test_composite_provider_falls_back_to_next_provider() -> None:
    working = _WorkingProvider()
    provider = CompositeMarketDataProvider([_FailingProvider(), working])
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = await provider.get_klines("BTCUSDT", "1m", start, start + timedelta(minutes=1))
    price = await provider.get_latest_price("BTCUSDT")

    assert len(candles) == 1
    assert working.calls == 1
    assert price == Decimal("123")


@pytest.mark.asyncio
async def test_composite_provider_emits_fallback_logs(caplog) -> None:
    caplog.set_level(logging.INFO, logger="triak_trade.market_data.composite")
    provider = CompositeMarketDataProvider([_FailingProvider(), _WorkingProvider()])
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    await provider.get_klines("BTCUSDT", "1m", start, start + timedelta(minutes=1))

    messages = [record.message for record in caplog.records]
    assert "market_data.composite_provider_failed" in messages
    assert "market_data.composite_provider_succeeded" in messages
