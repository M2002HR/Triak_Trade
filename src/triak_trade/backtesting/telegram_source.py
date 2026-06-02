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
    ) -> tuple[list[RawTelegramMessage], TelegramHistoryFetchResult]:
        messages = await self.telegram_client.fetch_history(
            channel,
            start=start,
            end=end,
            limit=limit,
        )
        return messages, TelegramHistoryFetchResult(
            channel=channel,
            fetched_count=len(messages),
            used_real_telegram=True,
            start=start,
            end=end,
        )
