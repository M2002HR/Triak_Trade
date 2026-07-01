from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from tempfile import NamedTemporaryFile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from triak_trade.db.base import Base
from triak_trade.db.repositories import TelegramMessageRepository
from triak_trade.domain.models import RawTelegramMessage
from triak_trade.telegram.client import FakeTelegramClient
from triak_trade.telegram.history_sync import TelegramHistorySyncService


def _session() -> Session:
    tmp = NamedTemporaryFile(suffix=".db")
    engine = create_engine(f"sqlite+pysqlite:///{tmp.name}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    session.info["_tmpfile"] = tmp
    return session


def _raw(
    mid: int,
    text: str,
    edited_at: datetime | None = None,
    deleted: bool = False,
) -> RawTelegramMessage:
    now = datetime.now(timezone.utc)
    return RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="tofan",
        message_id=mid,
        text=text,
        date=now,
        edited_at=edited_at,
        deleted=deleted,
        reply_to_msg_id=None,
        raw_payload={"sample": True},
    )


@pytest.mark.asyncio
async def test_history_sync_stores_messages_and_dedupes_and_versions() -> None:
    session = _session()
    original = _raw(10, "first")
    exact_dup = _raw(10, "first")
    exact_dup = exact_dup.model_copy(
        update={"date": original.date, "edited_at": original.edited_at}
    )
    edited = _raw(10, "first edit", edited_at=original.date + timedelta(seconds=1))
    deleted = _raw(10, "first edit", deleted=True)

    client = FakeTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [original, exact_dup, edited, deleted]}
    )
    svc = TelegramHistorySyncService(telegram_client=client, session=session)
    summary = await svc.sync_channel("https://t.me/Tofan_Trade")

    repo = TelegramMessageRepository(session)
    rows = repo.list_messages("https://t.me/Tofan_Trade")
    assert summary.fetched_count == 4
    assert summary.inserted_or_seen_count == 4
    assert len(rows) == 3
    assert rows[-1].deleted is True


@pytest.mark.asyncio
async def test_history_sync_emits_summary_logs(caplog) -> None:
    caplog.set_level(logging.INFO, logger="triak_trade.telegram.history_sync")
    session = _session()
    client = FakeTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [_raw(1, "first")]}
    )
    svc = TelegramHistorySyncService(telegram_client=client, session=session)

    await svc.sync_channel("https://t.me/Tofan_Trade")

    messages = [record.message for record in caplog.records]
    assert "telegram_history_sync.started" in messages
    assert "telegram_history_sync.completed" in messages
