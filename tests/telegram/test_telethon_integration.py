from __future__ import annotations

import os

import pytest

from triak_trade.config.settings import Settings
from triak_trade.telegram.telethon_client import TelethonTelegramClient


@pytest.mark.asyncio
async def test_optional_telethon_fetch_integration() -> None:
    if os.getenv("RUN_TELEGRAM_INTEGRATION_TESTS") != "1":
        pytest.skip("guard disabled")

    settings = Settings()
    if (
        settings.TELEGRAM_API_ID <= 0
        or settings.TELEGRAM_API_HASH.get_secret_value() == "replace_me"
    ):
        pytest.skip("telegram credentials missing")

    client = TelethonTelegramClient(settings)
    try:
        result = await client.fetch_history(settings.TELEGRAM_REAL_TEST_CHANNEL, limit=1)
    except Exception as exc:  # pragma: no cover
        lowered = str(exc).lower()
        assert "api" in lowered or "auth" in lowered or "telegram" in lowered
    else:
        assert isinstance(result, list)
