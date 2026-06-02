"""Telegram Bot API wrapper for admin actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from triak_trade.admin_bot.callbacks import parse_admin_callback
from triak_trade.admin_bot.errors import AdminRegistrationError
from triak_trade.admin_bot.formatter import AdminActionFormatter, FormattedAdminAction
from triak_trade.domain.models import AdminDecision, ProposedAction, SignalState


@dataclass
class AdminChatRegistration:
    username: str
    chat_id: int
    first_seen_at: datetime
    last_seen_at: datetime


class TelegramAdminBot:
    def __init__(
        self,
        *,
        bot_token: str,
        parse_mode: str,
        disable_web_preview: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.parse_mode = parse_mode
        self.disable_web_preview = disable_web_preview
        self.transport = transport
        self.formatter = AdminActionFormatter()
        self.registrations: dict[str, AdminChatRegistration] = {}

    def handle_start(self, username: str | None, chat_id: int) -> AdminChatRegistration:
        if username is None or not username.strip():
            raise AdminRegistrationError("username is required for registration")
        normalized = username.strip().lstrip("@").lower()
        now = datetime.now(timezone.utc)
        existing = self.registrations.get(normalized)
        if existing is None:
            reg = AdminChatRegistration(
                username=normalized,
                chat_id=chat_id,
                first_seen_at=now,
                last_seen_at=now,
            )
            self.registrations[normalized] = reg
            return reg
        existing.chat_id = chat_id
        existing.last_seen_at = now
        return existing

    async def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        api = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": self.parse_mode,
            "disable_web_page_preview": self.disable_web_preview,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[button for button in buttons]]}

        async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
            response = await client.post(api, json=payload)
        if response.status_code >= 400:
            raise AdminRegistrationError("telegram sendMessage failed")
        try:
            parsed = response.json()
        except ValueError as exc:
            raise AdminRegistrationError("telegram response parse failed") from exc
        if not isinstance(parsed, dict):
            raise AdminRegistrationError("telegram response must be object")
        return parsed

    async def send_proposed_action(
        self,
        chat_id: int,
        action: ProposedAction,
        signal: SignalState | None = None,
        metrics: dict[str, Any] | None = None,
        risk: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        formatted: FormattedAdminAction = self.formatter.format_action(
            action,
            signal,
            metrics,
            risk,
        )
        buttons = [
            {"text": button.text, "callback_data": button.callback_data}
            for button in formatted.buttons
        ]
        return await self.send_message(chat_id=chat_id, text=formatted.text, buttons=buttons)

    async def send_test_message(self, chat_id: int, text: str) -> dict[str, Any]:
        return await self.send_message(chat_id=chat_id, text=text, buttons=None)

    def handle_callback(self, username: str, callback_data: str) -> AdminDecision:
        parsed = parse_admin_callback(callback_data)
        return AdminDecision(
            action_id=parsed.action_id,
            decision=parsed.decision,
            admin_user_id=1,
            reason=f"callback by {username}",
            decided_at=datetime.now(timezone.utc),
        )
