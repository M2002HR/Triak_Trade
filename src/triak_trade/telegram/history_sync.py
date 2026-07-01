"""History synchronization service."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from triak_trade.core.logging import duration_ms, log_event
from triak_trade.db.repositories import TelegramMessageRepository
from triak_trade.telegram.client import TelegramClientInterface

_log = logging.getLogger(__name__)


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
        started_at = datetime.now()
        errors: list[str] = []
        inserted_or_seen_count = 0
        log_event(
            _log,
            logging.INFO,
            "telegram_history_sync.started",
            channel=channel,
            start=start.isoformat() if start is not None else None,
            end=end.isoformat() if end is not None else None,
            limit=limit,
        )
        messages = await self.telegram_client.fetch_history(
            channel,
            start=start,
            end=end,
            limit=limit,
        )
        log_event(
            _log,
            logging.DEBUG,
            "telegram_history_sync.fetched",
            channel=channel,
            fetched_count=len(messages),
        )
        try:
            for message in messages:
                self.repo.add_raw_message(message)
                inserted_or_seen_count += 1
            self.session.commit()
        except Exception as exc:  # pragma: no cover
            self.session.rollback()
            errors.append(str(exc))
            log_event(
                _log,
                logging.ERROR,
                "telegram_history_sync.failed",
                channel=channel,
                fetched_count=len(messages),
                inserted_or_seen_count=inserted_or_seen_count,
                error_type=type(exc).__name__,
                error=str(exc),
            )

        finished_at = datetime.now()
        log_event(
            _log,
            logging.INFO,
            "telegram_history_sync.completed",
            channel=channel,
            fetched_count=len(messages),
            inserted_or_seen_count=inserted_or_seen_count,
            error_count=len(errors),
            duration_ms=duration_ms(started_at, finished_at),
        )

        return HistorySyncSummary(
            channel=channel,
            fetched_count=len(messages),
            inserted_or_seen_count=inserted_or_seen_count,
            start=start,
            end=end,
            errors=errors,
        )
