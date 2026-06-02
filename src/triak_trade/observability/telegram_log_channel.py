"""Guarded Telegram log-channel sender."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx

from triak_trade.config.settings import Settings
from triak_trade.observability.errors import TelegramLogChannelError
from triak_trade.observability.events import ProcessingAuditEvent
from triak_trade.observability.formatters import format_processing_audit_for_telegram
from triak_trade.observability.redaction import redact


@dataclass(frozen=True)
class TelegramLogSendResult:
    sent: bool
    skipped: bool
    reason: str
    message_id: int | None = None


class TelegramLogChannelClient:
    def __init__(
        self,
        *,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    def enabled_for_real_send(self) -> bool:
        token = self.settings.TELEGRAM_BOT_TOKEN.get_secret_value()
        return (
            self.settings.TELEGRAM_LOG_CHANNEL_ENABLED
            and self.settings.PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL
            and self.settings.RUN_TELEGRAM_LOG_CHANNEL_INTEGRATION_TESTS == 1
            and bool(token and token != "replace_me")
        )

    async def send_event(
        self,
        event: ProcessingAuditEvent,
        *,
        real: bool = False,
    ) -> TelegramLogSendResult:
        if not real:
            return TelegramLogSendResult(sent=False, skipped=True, reason="real flag not set")
        if not self.enabled_for_real_send():
            return TelegramLogSendResult(
                sent=False,
                skipped=True,
                reason="log channel guard disabled",
            )
        return await self.send_text(format_processing_audit_for_telegram(event), real=True)

    async def send_text(self, text: str, *, real: bool = False) -> TelegramLogSendResult:
        if not real:
            return TelegramLogSendResult(sent=False, skipped=True, reason="real flag not set")
        if not self.enabled_for_real_send():
            return TelegramLogSendResult(
                sent=False,
                skipped=True,
                reason="log channel guard disabled",
            )

        token = self.settings.TELEGRAM_BOT_TOKEN.get_secret_value()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": self.settings.TELEGRAM_LOG_CHANNEL_USERNAME,
            "text": text,
            "parse_mode": self.settings.TELEGRAM_LOG_CHANNEL_PARSE_MODE,
            "disable_web_page_preview": self.settings.TELEGRAM_LOG_CHANNEL_DISABLE_WEB_PAGE_PREVIEW,
        }
        try:
            async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            msg = f"telegram log send failed: {type(exc).__name__}"
            raise TelegramLogChannelError(msg) from exc
        if response.status_code >= 400:
            raise TelegramLogChannelError(
                f"telegram log send failed with status {response.status_code}"
            )
        try:
            parsed = response.json()
        except ValueError as exc:
            raise TelegramLogChannelError("telegram log response was not JSON") from exc
        if not isinstance(parsed, dict):
            raise TelegramLogChannelError("telegram log response must be object")
        result = parsed.get("result")
        message_id = None
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            message_id = result["message_id"]
        return TelegramLogSendResult(
            sent=True,
            skipped=False,
            reason="sent",
            message_id=message_id,
        )

    def safe_status(self) -> dict[str, Any]:
        token = self.settings.TELEGRAM_BOT_TOKEN.get_secret_value()
        payload = redact(
            {
                "log_channel_username": self.settings.TELEGRAM_LOG_CHANNEL_USERNAME,
                "enabled": self.settings.TELEGRAM_LOG_CHANNEL_ENABLED,
                "send_full_text": self.settings.TELEGRAM_LOG_CHANNEL_SEND_FULL_TEXT,
                "bot_token_present": bool(token and token != "replace_me"),
                "guard_enabled": self.settings.RUN_TELEGRAM_LOG_CHANNEL_INTEGRATION_TESTS == 1,
                "audit_send_enabled": self.settings.PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL,
            }
        )
        return cast(dict[str, Any], payload)
