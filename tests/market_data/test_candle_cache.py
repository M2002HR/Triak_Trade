from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from tempfile import NamedTemporaryFile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from triak_trade.db.base import Base
from triak_trade.db.repositories import CandleRepository
from triak_trade.domain.enums import CandleSource
from triak_trade.domain.models import Candle
from triak_trade.market_data.candle_cache import CandleCacheService


class FakeProvider:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles
        self.calls = 0

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        self.calls += 1
        return [c for c in self.candles if start_time <= c.open_time <= end_time]

    async def get_latest_price(self, symbol: str) -> Decimal:
        raise NotImplementedError


def _session() -> Session:
    tmp = NamedTemporaryFile(suffix=".db")
    engine = create_engine(f"sqlite+pysqlite:///{tmp.name}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    session.info["_tmpfile"] = tmp
    return session


def _candles() -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result: list[Candle] = []
    for i in range(5):
        open_time = start + timedelta(minutes=i)
        result.append(
            Candle(
                symbol="BTCUSDT",
                interval="1m",
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open=Decimal("100") + Decimal(str(i)),
                high=Decimal("101") + Decimal(str(i)),
                low=Decimal("99") + Decimal(str(i)),
                close=Decimal("100.5") + Decimal(str(i)),
                volume=Decimal("10"),
                source=CandleSource.TOOBIT,
            )
        )
    return result


@pytest.mark.asyncio
async def test_cache_fetches_when_empty_and_stores_and_reuses() -> None:
    session = _session()
    repo = CandleRepository(session)
    candles = _candles()
    provider = FakeProvider(candles)
    cache = CandleCacheService(provider=provider, repository=repo)

    result1 = await cache.get_or_fetch_klines(
        "BTCUSDT",
        "1m",
        candles[0].open_time,
        candles[-1].open_time,
    )
    assert len(result1) == 5
    assert provider.calls >= 1

    calls_before = provider.calls
    result2 = await cache.get_or_fetch_klines(
        "BTCUSDT",
        "1m",
        candles[0].open_time,
        candles[-1].open_time,
    )
    assert len(result2) == 5
    assert provider.calls == calls_before


@pytest.mark.asyncio
async def test_cache_partial_fetch_and_no_duplicates() -> None:
    session = _session()
    repo = CandleRepository(session)
    candles = _candles()
    repo.upsert_candles(candles[1:4])
    provider = FakeProvider(candles)
    cache = CandleCacheService(provider=provider, repository=repo)

    result = await cache.get_or_fetch_klines(
        "BTCUSDT",
        "1m",
        candles[0].open_time,
        candles[-1].open_time,
    )
    assert len(result) == 5
    assert len({c.open_time for c in result}) == 5
    assert provider.calls >= 1
