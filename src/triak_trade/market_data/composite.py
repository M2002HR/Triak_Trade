"""Composite market-data provider with ordered fallback."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from triak_trade.core.logging import log_event
from triak_trade.domain.models import Candle
from triak_trade.market_data.errors import MarketDataError
from triak_trade.market_data.interfaces import MarketDataProvider

_log = logging.getLogger(__name__)


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
                log_event(
                    _log,
                    logging.WARNING,
                    "market_data.composite_provider_failed",
                    provider=provider.__class__.__name__,
                    symbol=symbol,
                    interval=interval,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                continue
            if candles:
                log_event(
                    _log,
                    logging.INFO,
                    "market_data.composite_provider_succeeded",
                    provider=provider.__class__.__name__,
                    symbol=symbol,
                    interval=interval,
                    candle_count=len(candles),
                )
                return candles
        if last_error is not None:
            raise last_error
        return []

    async def get_latest_price(self, symbol: str) -> Decimal:
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                price = await provider.get_latest_price(symbol)
                log_event(
                    _log,
                    logging.INFO,
                    "market_data.composite_latest_price_succeeded",
                    provider=provider.__class__.__name__,
                    symbol=symbol,
                    price=str(price),
                )
                return price
            except MarketDataError as exc:
                last_error = exc
                log_event(
                    _log,
                    logging.WARNING,
                    "market_data.composite_latest_price_failed",
                    provider=provider.__class__.__name__,
                    symbol=symbol,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                continue
        if last_error is not None:
            raise last_error
        raise MarketDataError("No market-data provider could return latest price")
