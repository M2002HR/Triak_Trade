from __future__ import annotations

import logging

import httpx
import pytest

from triak_trade.config.settings import Settings
from triak_trade.observability.processing_audit import build_sample_processing_audit_event
from triak_trade.observability.telegram_log_channel import TelegramLogChannelClient
from triak_trade.verification.redaction import redact_text


def disabled_settings() -> Settings:
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="123456789:abcdefghijklmnopqrstuvwxyzABCDEFG",
        TELEGRAM_LOG_CHANNEL_ENABLED=False,
        PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL=False,
        RUN_TELEGRAM_LOG_CHANNEL_INTEGRATION_TESTS=0,
    )


def enabled_settings() -> Settings:
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="123456789:abcdefghijklmnopqrstuvwxyzABCDEFG",
        TELEGRAM_LOG_CHANNEL_ENABLED=True,
        PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL=True,
        RUN_TELEGRAM_LOG_CHANNEL_INTEGRATION_TESTS=1,
    )


@pytest.mark.asyncio
async def test_log_channel_client_skips_when_disabled() -> None:
    client = TelegramLogChannelClient(settings=disabled_settings())

    result = await client.send_event(
        build_sample_processing_audit_event(disabled_settings()),
        real=True,
    )

    assert result.skipped is True
    assert result.sent is False


@pytest.mark.asyncio
async def test_log_channel_client_sends_expected_request_with_mock_transport() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    client = TelegramLogChannelClient(
        settings=enabled_settings(),
        transport=httpx.MockTransport(handler),
    )

    result = await client.send_event(
        build_sample_processing_audit_event(enabled_settings()),
        real=True,
    )

    assert result.sent is True
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert result.message_id == 77
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/sendMessage")
    payload = requests[0].read().decode()
    assert "@triak_logs" in payload
    assert "Message Processing Report" in payload


def test_log_channel_status_does_not_include_token() -> None:
    client = TelegramLogChannelClient(settings=enabled_settings())
    status = client.safe_status()

    assert status["bot_token_present"] is True
    assert "123456789:abcdefghijklmnopqrstuvwxyz" not in str(status)


def test_redaction_handles_bot_api_url() -> None:
    text = "https://api.telegram.org/bot123456789:abcdefghijklmnopqrstuvwxyzABCDEFG/sendMessage"
    redacted = redact_text(text)

    assert "bot123456789:abcdefghijklmnopqrstuvwxyzABCDEFG" not in redacted
    assert "***REDACTED***" in redacted
