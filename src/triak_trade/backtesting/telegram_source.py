"""Telegram history source helpers for real backtesting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from triak_trade.domain.models import RawTelegramMessage
from triak_trade.telegram.client import TelegramClientInterface


@dataclass(frozen=True)
class TelegramHistoryFetchResult:
    channel: str
    fetched_count: int
    used_real_telegram: bool
    start: datetime
    end: datetime


class BacktestTelegramSource:
    def __init__(self, telegram_client: TelegramClientInterface) -> None:
        self.telegram_client = telegram_client

    async def fetch(
        self,
        *,
        channel: str,
        start: datetime,
        end: datetime,
        limit: int,
        start_message_id: int | None = None,
    ) -> tuple[list[RawTelegramMessage], TelegramHistoryFetchResult]:
        # A Telegram message link is an explicit processing anchor. In that mode
        # the UI date range still bounds reporting/market replay, but it must not
        # hide the anchor message or messages after it.
        effective_start = None if start_message_id is not None else start
        messages = await self.telegram_client.fetch_history(
            channel,
            start=effective_start,
            end=end,
            limit=limit,
            min_message_id=start_message_id,
        )
        if start_message_id is not None:
            messages = [message for message in messages if message.message_id >= start_message_id]
        return messages, TelegramHistoryFetchResult(
            channel=channel,
            fetched_count=len(messages),
            used_real_telegram=True,
            start=start,
            end=end,
        )
