"""Toobit public market data provider."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from triak_trade.domain.enums import CandleSource
from triak_trade.domain.models import Candle
from triak_trade.market_data.errors import (
    MarketDataConnectionError,
    MarketDataHTTPError,
    MarketDataParseError,
    MarketDataTimeoutError,
)
from triak_trade.market_data.intervals import interval_to_milliseconds, validate_interval


class ToobitMarketDataProvider:
    def __init__(
        self,
        *,
        base_url: str,
        klines_path: str,
        timeout_seconds: int,
        limit: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.klines_path = klines_path
        self.timeout_seconds = timeout_seconds
        self.limit = limit
        self.transport = transport

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        normalized_symbol = symbol.strip().upper()
        normalized_interval = validate_interval(interval)
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        if end_ms <= start_ms:
            return []

        chunk_ms = interval_to_milliseconds(normalized_interval) * self.limit
        all_candles: dict[tuple[str, str, datetime, str], Candle] = {}

        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            cursor = start_ms
            while cursor < end_ms:
                chunk_end = min(end_ms, cursor + chunk_ms)
                rows = await self._fetch_chunk(
                    client=client,
                    symbol=normalized_symbol,
                    interval=normalized_interval,
                    start_ms=cursor,
                    end_ms=chunk_end,
                )
                for row in rows:
                    candle = self._parse_candle_row(
                        row=row,
                        symbol=normalized_symbol,
                        interval=normalized_interval,
                    )
                    key = (
                        candle.symbol,
                        candle.interval,
                        candle.open_time,
                        candle.source.value,
                    )
                    all_candles[key] = candle
                cursor = chunk_end

        return sorted(all_candles.values(), key=lambda item: item.open_time)

    async def get_latest_price(self, symbol: str) -> Decimal:
        raise NotImplementedError("Latest price endpoint is not implemented yet")

    async def _fetch_chunk(
        self,
        *,
        client: httpx.AsyncClient,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[Any]:
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": self.limit,
        }
        try:
            response = await client.get(self.klines_path, params=params)
        except httpx.TimeoutException as exc:
            raise MarketDataTimeoutError("Toobit market data request timed out") from exc
        except httpx.ConnectError as exc:
            raise MarketDataConnectionError("Toobit market data connection failed") from exc

        if response.status_code >= 400:
            raise MarketDataHTTPError(f"Toobit market data HTTP error: {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataParseError("Toobit market data JSON parse error") from exc

        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data
            result = payload.get("result")
            if isinstance(result, list):
                return result
            if data is None and result is None:
                return []
        raise MarketDataParseError("Unsupported Toobit kline payload format")

    @staticmethod
    def _parse_candle_row(*, row: Any, symbol: str, interval: str) -> Candle:
        if not isinstance(row, list) or len(row) < 6:
            raise MarketDataParseError("Invalid kline row format")
        try:
            open_time_ms = int(str(row[0]))
            open_price = Decimal(str(row[1]))
            high_price = Decimal(str(row[2]))
            low_price = Decimal(str(row[3]))
            close_price = Decimal(str(row[4]))
            volume = Decimal(str(row[5]))
            close_time_ms = int(str(row[6])) if len(row) > 6 and row[6] is not None else None
        except Exception as exc:
            raise MarketDataParseError("Invalid numeric kline values") from exc

        open_time = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
        if close_time_ms is None:
            close_time = open_time + timedelta(milliseconds=interval_to_milliseconds(interval))
        else:
            close_time = datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc)

        return Candle(
            symbol=symbol,
            interval=interval,
            open_time=open_time,
            close_time=close_time,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
            source=CandleSource.TOOBIT,
        )
