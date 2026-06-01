"""Candle cache service backed by repository."""

from __future__ import annotations

from datetime import datetime, timezone

from triak_trade.db.repositories import CandleRepository
from triak_trade.domain.models import Candle
from triak_trade.market_data.interfaces import MarketDataProvider


class CandleCacheService:
    def __init__(self, *, provider: MarketDataProvider, repository: CandleRepository) -> None:
        self.provider = provider
        self.repository = repository

    async def get_or_fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        cached = self.repository.list_candles(symbol, interval, start_time, end_time)
        normalized_symbol = symbol.strip().upper()
        if not cached:
            fetched = await self.provider.get_klines(
                normalized_symbol,
                interval,
                start_time,
                end_time,
            )
            self.repository.upsert_candles(fetched)
            return sorted(fetched, key=lambda item: item.open_time)

        cached_sorted = sorted(cached, key=lambda item: item.open_time)
        fetch_batches: list[list[Candle]] = []

        if _as_utc(start_time) < _as_utc(cached_sorted[0].open_time):
            before_end = _align_boundary(cached_sorted[0].open_time, start_time)
            fetch_batches.append(
                await self.provider.get_klines(
                    normalized_symbol,
                    interval,
                    start_time,
                    before_end,
                )
            )

        if _as_utc(end_time) > _as_utc(cached_sorted[-1].open_time):
            after_start = _align_boundary(cached_sorted[-1].open_time, end_time)
            fetch_batches.append(
                await self.provider.get_klines(
                    normalized_symbol,
                    interval,
                    after_start,
                    end_time,
                )
            )

        for batch in fetch_batches:
            self.repository.upsert_candles(batch)

        merged = self.repository.list_candles(normalized_symbol, interval, start_time, end_time)
        unique: dict[tuple[str, str, datetime, str], Candle] = {}
        for candle in merged:
            key = (candle.symbol, candle.interval, candle.open_time, candle.source.value)
            unique[key] = candle
        return sorted(unique.values(), key=lambda item: item.open_time)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _align_boundary(boundary: datetime, reference: datetime) -> datetime:
    if boundary.tzinfo is not None:
        return boundary
    if reference.tzinfo is None:
        return boundary
    return boundary.replace(tzinfo=timezone.utc)
