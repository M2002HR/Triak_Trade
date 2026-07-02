from __future__ import annotations

from datetime import datetime, timezone

import pytest

from triak_trade.config.settings import Settings
from triak_trade.domain.models import RawTelegramMessage
from triak_trade.telegram.shared_client import SharedTelethonTelegramClient


@pytest.mark.asyncio
async def test_shared_client_reuses_single_underlying_telethon_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, TELEGRAM_API_ID=123, TELEGRAM_API_HASH="hash")
    client = SharedTelethonTelegramClient(settings)
    seen_ids: list[int] = []

    async def _fake_fetch_history(
        channel: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        min_message_id: int | None = None,
    ) -> list[RawTelegramMessage]:
        seen_ids.append(id(client._client))
        return [
            RawTelegramMessage(
                channel_id=channel,
                channel_username="demo",
                message_id=min_message_id or 1,
                text="x",
                date=datetime.now(timezone.utc),
                edited_at=None,
                reply_to_msg_id=None,
            )
        ]

    monkeypatch.setattr(client._client, "fetch_history", _fake_fetch_history)

    first = await client.fetch_history("https://t.me/one", min_message_id=10)
    second = await client.fetch_history("https://t.me/two", min_message_id=20)

    assert first[0].message_id == 10
    assert second[0].message_id == 20
    assert len(set(seen_ids)) == 1

    await client.aclose()
