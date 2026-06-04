"""Composite market-data provider with ordered fallback."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from triak_trade.domain.models import Candle
from triak_trade.market_data.errors import MarketDataError
from triak_trade.market_data.interfaces import MarketDataProvider


class CompositeMarketDataProvider:
    def __init__(self, providers: list[MarketDataProvider]) -> None:
        self.providers = providers

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                candles = await provider.get_klines(symbol, interval, start_time, end_time)
            except MarketDataError as exc:
                last_error = exc
                continue
            if candles:
                return candles
        if last_error is not None:
            raise last_error
        return []

    async def get_latest_price(self, symbol: str) -> Decimal:
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                return await provider.get_latest_price(symbol)
            except MarketDataError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise MarketDataError("No market-data provider could return latest price")
