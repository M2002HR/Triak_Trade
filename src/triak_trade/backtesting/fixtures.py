"""Backtesting fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from triak_trade.domain.enums import CandleSource
from triak_trade.domain.models import Candle, RawTelegramMessage


def fixture_messages(channel: str = "https://t.me/Tofan_Trade") -> list[RawTelegramMessage]:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return [
        RawTelegramMessage(
            channel_id=channel,
            channel_username="tofan_trade",
            message_id=1,
            text="BTCUSDT LONG Entry: 100 - 101 SL: 98 TP: 104",
            date=start,
            edited_at=None,
            reply_to_msg_id=None,
        ),
        RawTelegramMessage(
            channel_id=channel,
            channel_username="tofan_trade",
            message_id=2,
            text="TP updated to 105",
            date=start + timedelta(minutes=1),
            edited_at=None,
            reply_to_msg_id=1,
        ),
        RawTelegramMessage(
            channel_id=channel,
            channel_username="tofan_trade",
            message_id=3,
            text="promo giveaway",
            date=start + timedelta(minutes=2),
            edited_at=None,
            reply_to_msg_id=None,
        ),
    ]


def fixture_candles(symbol: str = "BTCUSDT", interval: str = "1m") -> list[Candle]:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    values = [
        (Decimal("100"), Decimal("102"), Decimal("99"), Decimal("101")),
        (Decimal("101"), Decimal("104"), Decimal("100"), Decimal("103")),
        (Decimal("103"), Decimal("106"), Decimal("102"), Decimal("105")),
    ]
    out: list[Candle] = []
    for i, (open_price, high_price, low_price, close_price) in enumerate(values):
        t = start + timedelta(minutes=i)
        out.append(
            Candle(
                symbol=symbol,
                interval=interval,
                open_time=t,
                close_time=t + timedelta(minutes=1),
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=Decimal("10"),
                source=CandleSource.FIXTURE,
            )
        )
    return out
