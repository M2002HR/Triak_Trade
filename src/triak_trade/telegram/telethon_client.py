"""Telethon-backed Telegram client."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from triak_trade.config.settings import Settings
from triak_trade.domain.models import RawTelegramMessage
from triak_trade.telegram.mapper import telethon_message_to_raw


class TelegramCredentialError(ValueError):
    """Raised when Telegram credentials are missing for real client usage."""


class TelethonTelegramClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any | None = None
        self._message_cache: dict[tuple[str, int], Any] = {}

    @property
    def session_path(self) -> Path:
        return Path(self.settings.TELEGRAM_SESSION_DIR) / self.settings.TELEGRAM_SESSION_NAME

    def _validate_credentials(self) -> None:
        if self.settings.TELEGRAM_API_ID <= 0:
            raise TelegramCredentialError("TELEGRAM_API_ID is missing or invalid")
        api_hash = self.settings.TELEGRAM_API_HASH.get_secret_value()
        if not api_hash or api_hash == "replace_me":
            raise TelegramCredentialError("TELEGRAM_API_HASH is missing")

    def _build_client(self) -> Any:
        self._validate_credentials()
        try:
            from telethon import TelegramClient  # type: ignore[import-untyped]
            from telethon.sessions import StringSession  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("telethon is not installed") from exc

        string_session = self.settings.TELEGRAM_STRING_SESSION.get_secret_value().strip()
        if string_session:
            session: str | StringSession = StringSession(string_session)
        else:
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            session = str(self.session_path)
        return TelegramClient(
            session,
            self.settings.TELEGRAM_API_ID,
            self.settings.TELEGRAM_API_HASH.get_secret_value(),
            proxy=self._proxy_tuple(),
        )

    def _proxy_tuple(self) -> tuple[str, str, int, bool, str | None, str | None] | None:
        if not self.settings.TELEGRAM_PROXY_ENABLED:
            return None
        host = self.settings.TELEGRAM_PROXY_HOST.strip()
        port = self.settings.TELEGRAM_PROXY_PORT
        if not host or port <= 0:
            raise TelegramCredentialError(
                "TELEGRAM_PROXY_ENABLED=true requires TELEGRAM_PROXY_HOST and TELEGRAM_PROXY_PORT"
            )
        username = self.settings.TELEGRAM_PROXY_USERNAME.strip() or None
        password_value = self.settings.TELEGRAM_PROXY_PASSWORD.get_secret_value().strip()
        password = password_value or None
        return (
            self.settings.TELEGRAM_PROXY_TYPE.strip().lower(),
            host,
            port,
            self.settings.TELEGRAM_PROXY_RDNS,
            username,
            password,
        )

    async def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def fetch_history(
        self,
        channel: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[RawTelegramMessage]:
        client = await self._ensure_client()
        result: list[RawTelegramMessage] = []
        async with client:
            async for msg in client.iter_messages(channel, limit=limit):
                raw = telethon_message_to_raw(msg, channel=channel)
                self._cache_message(raw, msg)
                if start is not None and raw.date < start:
                    continue
                if end is not None and raw.date > end:
                    continue
                result.append(raw)
        result.sort(key=lambda item: item.date)
        return result

    async def listen_new_messages(
        self,
        channels: list[str],
        handler: Callable[[RawTelegramMessage], Awaitable[None]],
    ) -> None:
        client = await self._ensure_client()
        try:
            from telethon import events
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("telethon is not installed") from exc

        async def _on_message(event: Any) -> None:
            raw = telethon_message_to_raw(event.message)
            self._cache_message(raw, event.message)
            await handler(raw)
        client.add_event_handler(
            _on_message,
            events.NewMessage(chats=channels if channels else None),
        )

        async with client:
            await client.run_until_disconnected()

    async def ensure_media_payload(self, message: RawTelegramMessage) -> RawTelegramMessage:
        payload = dict(message.raw_payload)
        if not self.settings.TELEGRAM_MEDIA_DOWNLOAD_ENABLED:
            return message
        if not bool(payload.get("has_media")):
            return message
        if not bool(payload.get("caption_present")):
            payload["media_download_skipped"] = "no_caption"
            return message.model_copy(update={"raw_payload": payload})
        if payload.get("image_data_urls"):
            return message

        key = (message.channel_id, message.message_id)
        source_message = self._message_cache.get(key)
        if source_message is None:
            payload["media_download_skipped"] = "source_not_cached"
            return message.model_copy(update={"raw_payload": payload})

        client = await self._ensure_client()
        is_connected = getattr(client, "is_connected", None)
        if callable(is_connected) and is_connected():
            return await self._hydrate_media_payload(client, source_message, message)
        async with client:
            return await self._hydrate_media_payload(client, source_message, message)

    def _cache_message(self, raw: RawTelegramMessage, source_message: Any) -> None:
        self._message_cache[(raw.channel_id, raw.message_id)] = source_message

    async def _hydrate_media_payload(
        self,
        client: Any,
        message: Any,
        raw: RawTelegramMessage,
    ) -> RawTelegramMessage:
        if not self.settings.TELEGRAM_MEDIA_DOWNLOAD_ENABLED:
            return raw
        payload = dict(raw.raw_payload)
        if not bool(payload.get("has_media")):
            return raw
        if not bool(payload.get("caption_present")):
            payload["media_download_skipped"] = "no_caption"
            return raw.model_copy(update={"raw_payload": payload})
        has_photo = bool(payload.get("has_photo"))
        mime_type = payload.get("mime_type")
        is_image_document = isinstance(mime_type, str) and mime_type.startswith("image/")
        if not has_photo and not is_image_document:
            payload["media_download_skipped"] = "not_supported_image"
            return raw.model_copy(update={"raw_payload": payload})
        if payload.get("image_data_urls"):
            return raw
        try:
            media_bytes = await client.download_media(message, file=bytes)
        except Exception:
            payload["media_download_skipped"] = "download_failed"
            return raw.model_copy(update={"raw_payload": payload})
        if not isinstance(media_bytes, (bytes, bytearray)) or not media_bytes:
            payload["media_download_skipped"] = "empty"
            return raw.model_copy(update={"raw_payload": payload})
        if len(media_bytes) > self.settings.TELEGRAM_MEDIA_MAX_BYTES:
            payload["media_bytes_skipped"] = "too_large"
            return raw.model_copy(update={"raw_payload": payload})
        effective_mime_type = "image/jpeg" if has_photo else str(mime_type or "image/jpeg")
        data_url = (
            f"data:{effective_mime_type};base64,"
            f"{base64.b64encode(bytes(media_bytes)).decode('ascii')}"
        )
        payload["image_data_urls"] = [
            {
                "mime_type": effective_mime_type,
                "data_url": data_url,
            }
        ][: self.settings.TELEGRAM_MEDIA_MAX_IMAGES]
        payload["media_downloaded"] = True
        return raw.model_copy(update={"raw_payload": payload})
