"""Admin approval workflow service."""

from __future__ import annotations

from typing import Any

from triak_trade.admin_bot.auth import AdminAuthService, normalize_username
from triak_trade.admin_bot.callbacks import parse_admin_callback
from triak_trade.admin_bot.errors import AdminRegistrationError
from triak_trade.admin_bot.telegram_bot import TelegramAdminBot
from triak_trade.db.repositories import AdminDecisionRepository
from triak_trade.domain.models import AdminDecision, ProposedAction, SignalState


class AdminApprovalService:
    def __init__(
        self,
        *,
        auth: AdminAuthService,
        bot: TelegramAdminBot,
        decisions: AdminDecisionRepository | None = None,
    ) -> None:
        self.auth = auth
        self.bot = bot
        self.decisions = decisions

    async def send_for_approval(
        self,
        action: ProposedAction,
        signal: SignalState | None = None,
    ) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []
        for username in sorted(self.auth.allowed_usernames):
            reg = self.bot.registrations.get(username)
            if reg is None:
                raise AdminRegistrationError("Admin must start the bot first.")
            response = await self.bot.send_proposed_action(reg.chat_id, action, signal)
            message_id = None
            result = response.get("result")
            if isinstance(result, dict):
                message_id = result.get("message_id")
            sent.append({"username": username, "message_id": message_id})
        return sent

    def handle_callback(self, username: str | None, callback_data: str) -> AdminDecision:
        self.auth.require_authorized_username(username)
        parsed = parse_admin_callback(callback_data)
        decision = self.bot.handle_callback(normalize_username(username or ""), callback_data)
        decision.action_id = parsed.action_id
        decision.decision = parsed.decision
        if self.decisions is not None:
            self.decisions.save_decision(decision)
        return decision
