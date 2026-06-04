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
    assert await client.ensure_media_payload(message) is message


def test_telethon_client_instantiation_and_missing_credentials() -> None:
    settings = Settings(TELEGRAM_API_ID=0, TELEGRAM_API_HASH="replace_me")
    client = TelethonTelegramClient(settings)
    assert str(client.session_path).endswith(".sessions/triak_trade")
    with pytest.raises(TelegramCredentialError):
        client._validate_credentials()


@pytest.mark.asyncio
async def test_telethon_client_ensure_media_payload_downloads_only_caption_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        TELEGRAM_API_ID=123,
        TELEGRAM_API_HASH="hash",
        TELEGRAM_MEDIA_DOWNLOAD_ENABLED=True,
    )
    client = TelethonTelegramClient(settings)

    class SourceMessage:
        pass

    source = SourceMessage()
    raw = RawTelegramMessage(
        channel_id="c",
        channel_username="u",
        message_id=7,
        text="caption",
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
        raw_payload={
            "has_media": True,
            "caption_present": True,
            "has_photo": True,
            "mime_type": "image/jpeg",
            "image_data_urls": [],
        },
    )
    client._cache_message(raw, source)

    class StubDownloader:
        def __init__(self) -> None:
            self.calls = 0

        def is_connected(self) -> bool:
            return True

        async def download_media(self, message: object, file: object = bytes) -> bytes:
            self.calls += 1
            assert message is source
            return b"fake-image-bytes"

    stub = StubDownloader()

    async def _return_stub() -> StubDownloader:
        return stub

    monkeypatch.setattr(client, "_ensure_client", _return_stub)

    hydrated = await client.ensure_media_payload(raw)

    assert stub.calls == 1
    assert hydrated.raw_payload["media_downloaded"] is True
    assert hydrated.raw_payload["image_data_urls"]


@pytest.mark.asyncio
async def test_telethon_client_ensure_media_payload_skips_non_caption_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        TELEGRAM_API_ID=123,
        TELEGRAM_API_HASH="hash",
        TELEGRAM_MEDIA_DOWNLOAD_ENABLED=True,
    )
    client = TelethonTelegramClient(settings)
    raw = RawTelegramMessage(
        channel_id="c",
        channel_username="u",
        message_id=8,
        text=None,
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
        raw_payload={
            "has_media": True,
            "caption_present": False,
            "has_photo": True,
            "mime_type": "image/jpeg",
            "image_data_urls": [],
        },
    )

    async def _boom() -> object:
        raise AssertionError("ensure_client should not be called for non-caption media")

    monkeypatch.setattr(client, "_ensure_client", _boom)

    hydrated = await client.ensure_media_payload(raw)

    assert hydrated.raw_payload["media_download_skipped"] == "no_caption"


@pytest.mark.asyncio
async def test_telethon_client_ensure_media_payload_marks_download_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        TELEGRAM_API_ID=123,
        TELEGRAM_API_HASH="hash",
        TELEGRAM_MEDIA_DOWNLOAD_ENABLED=True,
    )
    client = TelethonTelegramClient(settings)

    class SourceMessage:
        pass

    source = SourceMessage()
    raw = RawTelegramMessage(
        channel_id="c",
        channel_username="u",
        message_id=9,
        text="caption",
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
        raw_payload={
            "has_media": True,
            "caption_present": True,
            "has_photo": True,
            "mime_type": "image/jpeg",
            "image_data_urls": [],
        },
    )
    client._cache_message(raw, source)

    class BrokenDownloader:
        def is_connected(self) -> bool:
            return True

        async def download_media(self, message: object, file: object = bytes) -> bytes:
            raise RuntimeError("boom")

    async def _return_broken() -> BrokenDownloader:
        return BrokenDownloader()

    monkeypatch.setattr(client, "_ensure_client", _return_broken)

    hydrated = await client.ensure_media_payload(raw)

    assert hydrated.raw_payload["media_download_skipped"] == "download_failed"
