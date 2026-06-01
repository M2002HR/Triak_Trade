from __future__ import annotations

from datetime import datetime, timezone

import pytest

from triak_trade.config.settings import Settings
from triak_trade.domain.models import RawTelegramMessage
from triak_trade.telegram.client import FakeTelegramClient
from triak_trade.telegram.telethon_client import TelegramCredentialError, TelethonTelegramClient


@pytest.mark.asyncio
async def test_fake_client_fetch_history_contract() -> None:
    message = RawTelegramMessage(
        channel_id="c",
        channel_username="u",
        message_id=1,
        text="x",
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
    )
    client = FakeTelegramClient(history_by_channel={"c": [message]})
    got = await client.fetch_history("c", limit=1)
    assert len(got) == 1
    assert got[0].message_id == 1


def test_telethon_client_instantiation_and_missing_credentials() -> None:
    settings = Settings(TELEGRAM_API_ID=0, TELEGRAM_API_HASH="replace_me")
    client = TelethonTelegramClient(settings)
    assert str(client.session_path).endswith(".sessions/triak_trade")
    with pytest.raises(TelegramCredentialError):
        client._validate_credentials()
