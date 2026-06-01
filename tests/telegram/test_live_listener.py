from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from triak_trade.domain.enums import ProposedActionType
from triak_trade.domain.models import ProposedAction, RawTelegramMessage
from triak_trade.telegram.client import FakeTelegramClient
from triak_trade.telegram.live_listener import TelegramLiveListenerService


class FakeAgent:
    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id

    def ingest_message(self, raw_message: RawTelegramMessage) -> list[ProposedAction]:
        if raw_message.text and "ignore" in raw_message.text.lower():
            return []
        return [
            ProposedAction(
                action_id=f"{self.channel_id}-{raw_message.message_id}",
                action_type=ProposedActionType.REQUEST_ADMIN_CONFIRMATION,
                signal_id=None,
                risk_increasing=False,
                requires_admin_approval=True,
                confidence=Decimal("0.55"),
                reason="test",
                payload={"mid": raw_message.message_id},
                created_at=raw_message.date,
            )
        ]


@pytest.mark.asyncio
async def test_live_listener_routes_and_collects_actions() -> None:
    now = datetime.now(timezone.utc)
    msgs = [
        RawTelegramMessage(
            channel_id="chan-a",
            channel_username="a",
            message_id=1,
            text="hello",
            date=now,
            edited_at=None,
            reply_to_msg_id=None,
        ),
        RawTelegramMessage(
            channel_id="chan-a",
            channel_username="a",
            message_id=2,
            text="ignore this",
            date=now,
            edited_at=None,
            reply_to_msg_id=None,
        ),
    ]
    client = FakeTelegramClient(live_messages=msgs)
    collected: list[tuple[str, int]] = []

    async def on_actions(channel_id: str, actions: list[ProposedAction]) -> None:
        for action in actions:
            collected.append((channel_id, action.payload["mid"]))

    svc = TelegramLiveListenerService(
        telegram_client=client,
        agent_factory=lambda channel_id: FakeAgent(channel_id),
        on_actions=on_actions,
    )
    await svc.start(["chan-a"])
    assert collected == [("chan-a", 1)]
