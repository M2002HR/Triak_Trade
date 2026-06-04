from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from triak_trade.backtesting.telegram_source import BacktestTelegramSource
from triak_trade.domain.models import RawTelegramMessage
from triak_trade.telegram.client import FakeTelegramClient


def _message(message_id: int, when: datetime) -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=message_id,
        text=f"message {message_id}",
        date=when,
        edited_at=None,
        reply_to_msg_id=None,
    )


@pytest.mark.asyncio
async def test_backtest_telegram_source_starts_from_message_id() -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    messages = [
        _message(5878, now),
        _message(5879, now + timedelta(minutes=1)),
        _message(5880, now + timedelta(minutes=2)),
        _message(5881, now + timedelta(minutes=3)),
    ]
    source = BacktestTelegramSource(
        FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": messages})
    )

    fetched, meta = await source.fetch(
        channel="https://t.me/Tofan_Trade",
        start=now - timedelta(minutes=1),
        end=now + timedelta(minutes=10),
        limit=100,
        start_message_id=5880,
    )

    assert [item.message_id for item in fetched] == [5880, 5881]
    assert meta.fetched_count == 2


@pytest.mark.asyncio
async def test_backtest_telegram_source_applies_limit_after_start_message() -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    messages = [
        _message(5878, now),
        _message(5879, now + timedelta(minutes=1)),
        _message(5880, now + timedelta(minutes=2)),
        _message(5881, now + timedelta(minutes=3)),
    ]
    source = BacktestTelegramSource(
        FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": messages})
    )

    fetched, meta = await source.fetch(
        channel="https://t.me/Tofan_Trade",
        start=now - timedelta(minutes=1),
        end=now + timedelta(minutes=10),
        limit=1,
        start_message_id=5880,
    )

    assert [item.message_id for item in fetched] == [5880]
    assert meta.fetched_count == 1
