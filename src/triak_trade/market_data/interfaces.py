"""Market data provider interfaces."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from triak_trade.domain.models import Candle


class MarketDataProvider(Protocol):
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]: ...

    async def get_latest_price(self, symbol: str) -> Decimal: ...
