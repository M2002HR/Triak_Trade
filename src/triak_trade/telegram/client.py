"""Telegram client protocol and fake client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from triak_trade.domain.models import RawTelegramMessage


class TelegramClientInterface(Protocol):
    async def fetch_history(
        self,
        channel: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        min_message_id: int | None = None,
    ) -> list[RawTelegramMessage]: ...

    async def listen_new_messages(
        self,
        channels: list[str],
        handler: Callable[[RawTelegramMessage], Awaitable[None]],
    ) -> None: ...

    async def ensure_media_payload(self, message: RawTelegramMessage) -> RawTelegramMessage: ...


class FakeTelegramClient:
    def __init__(
        self,
        history_by_channel: dict[str, list[RawTelegramMessage]] | None = None,
        live_messages: list[RawTelegramMessage] | None = None,
    ) -> None:
        self.history_by_channel = history_by_channel or {}
        self.live_messages = live_messages or []

    async def fetch_history(
        self,
        channel: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        min_message_id: int | None = None,
    ) -> list[RawTelegramMessage]:
        messages = list(self.history_by_channel.get(channel, []))
        if start is not None:
            messages = [m for m in messages if m.date >= start]
        if end is not None:
            messages = [m for m in messages if m.date <= end]
        if min_message_id is not None:
            messages = [m for m in messages if m.message_id >= min_message_id]
        if limit is not None:
            messages = messages[:limit]
        return messages

    async def listen_new_messages(
        self,
        channels: list[str],
        handler: Callable[[RawTelegramMessage], Awaitable[None]],
    ) -> None:
        allowed = set(channels)
        for message in self.live_messages:
            if message.channel_id in allowed or not allowed:
                await handler(message)

    async def ensure_media_payload(self, message: RawTelegramMessage) -> RawTelegramMessage:
        return message
