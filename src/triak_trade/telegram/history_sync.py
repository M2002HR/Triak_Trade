"""History synchronization service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from triak_trade.db.repositories import TelegramMessageRepository
from triak_trade.telegram.client import TelegramClientInterface


@dataclass
class HistorySyncSummary:
    channel: str
    fetched_count: int
    inserted_or_seen_count: int
    start: datetime | None
    end: datetime | None
    errors: list[str]


class TelegramHistorySyncService:
    def __init__(
        self,
        *,
        telegram_client: TelegramClientInterface,
        session: Session,
    ) -> None:
        self.telegram_client = telegram_client
        self.repo = TelegramMessageRepository(session)
        self.session = session

    async def sync_channel(
        self,
        channel: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> HistorySyncSummary:
        errors: list[str] = []
        inserted_or_seen_count = 0
        messages = await self.telegram_client.fetch_history(
            channel,
            start=start,
            end=end,
            limit=limit,
        )
        try:
            for message in messages:
                self.repo.add_raw_message(message)
                inserted_or_seen_count += 1
            self.session.commit()
        except Exception as exc:  # pragma: no cover
            self.session.rollback()
            errors.append(str(exc))

        return HistorySyncSummary(
            channel=channel,
            fetched_count=len(messages),
            inserted_or_seen_count=inserted_or_seen_count,
            start=start,
            end=end,
            errors=errors,
        )
