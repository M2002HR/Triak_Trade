from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from triak_trade.admin_bot.errors import AdminRegistrationError
from triak_trade.admin_bot.telegram_bot import TelegramAdminBot
from triak_trade.domain.enums import ProposedActionType
from triak_trade.domain.models import ProposedAction


@pytest.mark.asyncio
async def test_send_message_request_and_error_paths() -> None:
    captured = {}

    def ok_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bot = TelegramAdminBot(
        bot_token="token",
        parse_mode="HTML",
        disable_web_preview=True,
        transport=httpx.MockTransport(ok_handler),
    )
    out = await bot.send_message(1, "hello")
    assert out["ok"] is True
    assert "token" in captured["url"]

    bad = TelegramAdminBot(
        bot_token="token",
        parse_mode="HTML",
        disable_web_preview=True,
        transport=httpx.MockTransport(lambda request: httpx.Response(500, json={})),
    )
    with pytest.raises(AdminRegistrationError):
        await bad.send_message(1, "hello")


@pytest.mark.asyncio
async def test_send_proposed_action_and_start_registration() -> None:
    bot = TelegramAdminBot(
        bot_token="token",
        parse_mode="HTML",
        disable_web_preview=True,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})
        ),
    )
    reg = bot.handle_start("@we_are_waiting_for_him", 123)
    assert reg.username == "we_are_waiting_for_him"

    action = ProposedAction(
        action_id="a",
        action_type=ProposedActionType.CREATE_ORDER,
        signal_id="s",
        risk_increasing=True,
        requires_admin_approval=True,
        confidence=Decimal("0.8"),
        reason="r",
        payload={"symbol": "BTCUSDT"},
        created_at=datetime.now(timezone.utc),
    )
    out = await bot.send_proposed_action(123, action)
    assert out["ok"] is True
