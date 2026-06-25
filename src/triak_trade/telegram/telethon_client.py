"""Telethon-backed Telegram client."""

from __future__ import annotations

import asyncio
import base64
import socket
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

        proxy = self._proxy_tuple()
        # Convert http proxy tuple to dict form that python_socks understands
        if proxy is not None and proxy[0] == "http":
            proxy_kwargs: dict[str, Any] = {
                "proxy_type": "http",
                "addr": proxy[1],
                "port": proxy[2],
                "rdns": proxy[3],
            }
            if proxy[4]:
                proxy_kwargs["username"] = proxy[4]
            if proxy[5]:
                proxy_kwargs["password"] = proxy[5]
            effective_proxy: Any = proxy_kwargs
        else:
            effective_proxy = proxy

        return TelegramClient(
            session,
            self.settings.TELEGRAM_API_ID,
            self.settings.TELEGRAM_API_HASH.get_secret_value(),
            proxy=effective_proxy,
        )

    def _proxy_tuple(self) -> tuple[str, str, int, bool, str | None, str | None] | None:
        if not self.settings.TELEGRAM_PROXY_ENABLED:
            return None

        import os
        # Inside Docker: prefer TELEGRAM_PROXY_HOST_DOCKER / TELEGRAM_PROXY_PORT_DOCKER
        in_docker = os.path.exists("/.dockerenv")
        if in_docker:
            docker_host = getattr(self.settings, "TELEGRAM_PROXY_HOST_DOCKER", "").strip()
            docker_port = getattr(self.settings, "TELEGRAM_PROXY_PORT_DOCKER", 0)
            if docker_host and docker_port > 0:
                host = self._resolve_proxy_host(docker_host)
                port = docker_port
            else:
                host = self._resolve_proxy_host(self.settings.TELEGRAM_PROXY_HOST.strip())
                port = self.settings.TELEGRAM_PROXY_PORT
        else:
            host = self._resolve_proxy_host(self.settings.TELEGRAM_PROXY_HOST.strip())
            port = self.settings.TELEGRAM_PROXY_PORT

        if not host or port <= 0:
            raise TelegramCredentialError(
                "TELEGRAM_PROXY_ENABLED=true requires TELEGRAM_PROXY_HOST and TELEGRAM_PROXY_PORT"
            )
        proxy_type = self.settings.TELEGRAM_PROXY_TYPE.strip().lower()
        # Telethon/python_socks supports: socks5, socks4, http
        # Map common aliases
        if proxy_type in ("http", "https", "http_connect"):
            proxy_type = "http"
        username = self.settings.TELEGRAM_PROXY_USERNAME.strip() or None
        password_value = self.settings.TELEGRAM_PROXY_PASSWORD.get_secret_value().strip()
        password = password_value or None
        return (
            proxy_type,
            host,
            port,
            self.settings.TELEGRAM_PROXY_RDNS,
            username,
            password,
        )

    def _resolve_proxy_host(self, host: str) -> str:
        if host != "host.docker.internal":
            return host
        try:
            socket.gethostbyname(host)
        except OSError:
            return "127.0.0.1"
        return host

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
        min_message_id: int | None = None,
    ) -> list[RawTelegramMessage]:
        client = await self._ensure_client()
        result: list[RawTelegramMessage] = []
        effective_min_id = max((min_message_id or 0) - 1, 0)
        iter_kwargs: dict[str, int] = {}
        if limit is not None and effective_min_id <= 0:
            iter_kwargs["limit"] = limit
        if effective_min_id > 0:
            iter_kwargs["min_id"] = effective_min_id
        async with client:
            async for msg in client.iter_messages(channel, **iter_kwargs):
                raw = telethon_message_to_raw(msg, channel=channel)
                self._cache_message(raw, msg)
                if start is not None and raw.date < start:
                    continue
                if end is not None and raw.date > end:
                    continue
                if min_message_id is not None and raw.message_id < min_message_id:
                    continue
                result.append(raw)
        result.sort(key=lambda item: item.date)
        if limit is not None:
            result = result[:limit]
        return result

    async def listen_new_messages(
        self,
        channels: list[str],
        handler: Callable[[RawTelegramMessage], Awaitable[None]],
    ) -> None:
        try:
            from telethon import events
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("telethon is not installed") from exc

        # Always build a fresh client for live listening (avoids stale state)
        self._client = self._build_client()
        client = self._client

        # Build reverse lookup: username → channel_input
        # So we can tag each message with the original channel URL
        channel_lookup: dict[str, str] = {}
        for ch in channels:
            slug = ch.rsplit("/", 1)[-1].lstrip("@").lower()
            if slug:
                channel_lookup[slug] = ch
            # Also store the raw input itself as a key
            channel_lookup[ch.lower()] = ch

        async def _on_message(event: Any) -> None:
            try:
                msg = event.message
                # Resolve the channel input from the chat entity
                chat = getattr(msg, "chat", None) or getattr(event, "chat", None)
                chat_username = getattr(chat, "username", None)
                channel_ref: str | None = None
                if chat_username:
                    channel_ref = channel_lookup.get(chat_username.lower())
                    if channel_ref is None:
                        channel_ref = f"https://t.me/{chat_username}"

                raw = telethon_message_to_raw(msg, channel=channel_ref)
                self._cache_message(raw, msg)
                import logging
                logging.getLogger(__name__).debug(
                    "New message %s from channel=%s text=%s",
                    raw.message_id, raw.channel_id,
                    (raw.text or "")[:60],
                )
                await handler(raw)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "Error handling Telegram message %s",
                    getattr(getattr(event, "message", None), "id", "?"),
                    exc_info=True,
                )

        client.add_event_handler(
            _on_message,
            events.NewMessage(chats=channels if channels else None),
        )

        try:
            await client.start()
            import logging
            _log = logging.getLogger(__name__)

            # Join any channels we're not yet a member of (required to receive updates)
            for ch in channels:
                try:
                    entity = await client.get_entity(ch)
                    left = getattr(entity, "left", False)
                    if left:
                        _log.info("Joining channel %s to receive updates", ch)
                        await client(
                            __import__(
                                "telethon.tl.functions.channels",
                                fromlist=["JoinChannelRequest"],
                            ).JoinChannelRequest(entity)
                        )
                        _log.info("Joined channel %s", ch)
                except Exception as exc:
                    _log.warning("Could not join channel %s: %s", ch, exc)

            _log.info("Telegram listener connected, watching %d channels", len(channels))
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            self._client = None

    async def stop(self) -> None:
        """Disconnect the active client (if any)."""
        client = self._client
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
            self._client = None

    async def ensure_media_payload(
        self,
        message: RawTelegramMessage,
        *,
        allow_captionless: bool = False,
    ) -> RawTelegramMessage:
        payload = dict(message.raw_payload)
        if not self.settings.TELEGRAM_MEDIA_DOWNLOAD_ENABLED:
            return message
        if not bool(payload.get("has_media")):
            return message
        if not bool(payload.get("caption_present")) and not allow_captionless:
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
            return await self._hydrate_media_payload(
                client,
                source_message,
                message,
                allow_captionless=allow_captionless,
            )
        async with client:
            return await self._hydrate_media_payload(
                client,
                source_message,
                message,
                allow_captionless=allow_captionless,
            )

    def _cache_message(self, raw: RawTelegramMessage, source_message: Any) -> None:
        self._message_cache[(raw.channel_id, raw.message_id)] = source_message

    async def _hydrate_media_payload(
        self,
        client: Any,
        message: Any,
        raw: RawTelegramMessage,
        *,
        allow_captionless: bool = False,
    ) -> RawTelegramMessage:
        if not self.settings.TELEGRAM_MEDIA_DOWNLOAD_ENABLED:
            return raw
        payload = dict(raw.raw_payload)
        if not bool(payload.get("has_media")):
            return raw
        if not bool(payload.get("caption_present")) and not allow_captionless:
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
