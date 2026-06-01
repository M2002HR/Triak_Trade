"""Telegram service factory helpers."""

from __future__ import annotations

from sqlalchemy.orm import Session

from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.config.settings import Settings
from triak_trade.telegram.history_sync import TelegramHistorySyncService
from triak_trade.telegram.live_listener import TelegramLiveListenerService
from triak_trade.telegram.telethon_client import TelethonTelegramClient


def build_telegram_history_sync_service(
    *,
    settings: Settings,
    session: Session,
) -> TelegramHistorySyncService:
    client = TelethonTelegramClient(settings)
    return TelegramHistorySyncService(telegram_client=client, session=session)


def build_telegram_live_listener_service(
    *,
    settings: Settings,
) -> TelegramLiveListenerService:
    client = TelethonTelegramClient(settings)

    def _factory(channel_id: str) -> ChannelAgent:
        return ChannelAgent(channel_id=channel_id, settings=settings)

    return TelegramLiveListenerService(telegram_client=client, agent_factory=_factory)
