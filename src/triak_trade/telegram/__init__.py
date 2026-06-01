"""Telegram collection infrastructure."""

from triak_trade.telegram.client import FakeTelegramClient, TelegramClientInterface
from triak_trade.telegram.history_sync import TelegramHistorySyncService
from triak_trade.telegram.live_listener import TelegramLiveListenerService
from triak_trade.telegram.telethon_client import TelethonTelegramClient

__all__ = [
    "FakeTelegramClient",
    "TelegramClientInterface",
    "TelegramHistorySyncService",
    "TelegramLiveListenerService",
    "TelethonTelegramClient",
]
