from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from triak_trade.admin_bot.auth import AdminAuthService
from triak_trade.admin_bot.errors import AdminRegistrationError, AdminUnauthorizedError
from triak_trade.admin_bot.service import AdminApprovalService
from triak_trade.admin_bot.telegram_bot import TelegramAdminBot
from triak_trade.domain.enums import ProposedActionType
from triak_trade.domain.models import ProposedAction


class FakeDecisionRepo:
    def __init__(self) -> None:
        self.saved: list[object] = []

    def save_decision(self, decision: object) -> None:
        self.saved.append(decision)


@pytest.mark.asyncio
async def test_service_rejects_unregistered_and_unauthorized_callback() -> None:
    bot = TelegramAdminBot(
        bot_token="token",
        parse_mode="HTML",
        disable_web_preview=True,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
    )
    service = AdminApprovalService(auth=AdminAuthService(["@we_are_waiting_for_him"]), bot=bot)

    action = ProposedAction(
        action_id="x",
        action_type=ProposedActionType.CREATE_ORDER,
        signal_id="s",
        risk_increasing=True,
        requires_admin_approval=True,
        confidence=Decimal("0.8"),
        reason="r",
        payload={"symbol": "BTCUSDT"},
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(AdminRegistrationError):
        await service.send_for_approval(action)

    with pytest.raises(AdminUnauthorizedError):
        service.handle_callback("@not_allowed", "admin:approve:x")


@pytest.mark.asyncio
async def test_service_records_callback_decisions() -> None:
    bot = TelegramAdminBot(
        bot_token="token",
        parse_mode="HTML",
        disable_web_preview=True,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        ),
    )
    bot.handle_start("@we_are_waiting_for_him", 1)
    repo = FakeDecisionRepo()
    service = AdminApprovalService(
        auth=AdminAuthService(["@we_are_waiting_for_him"]),
        bot=bot,
        decisions=repo,  # type: ignore[arg-type]
    )
    approve = service.handle_callback("@we_are_waiting_for_him", "admin:approve:a1")
    reject = service.handle_callback("@we_are_waiting_for_him", "admin:reject:a1")
    watch = service.handle_callback("@we_are_waiting_for_him", "admin:watch:a1")
    assert approve.decision.value == "approve"
    assert reject.decision.value == "reject"
    assert watch.decision.value == "watch_only"
    assert len(repo.saved) == 3
