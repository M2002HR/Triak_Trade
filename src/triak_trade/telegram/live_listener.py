"""Live message listener service."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.domain.models import ProposedAction, RawTelegramMessage
from triak_trade.telegram.client import TelegramClientInterface


class TelegramLiveListenerService:
    def __init__(
        self,
        *,
        telegram_client: TelegramClientInterface,
        agent_factory: Callable[[str], ChannelAgent],
        on_actions: Callable[[str, list[ProposedAction]], Awaitable[None] | None] | None = None,
    ) -> None:
        self.telegram_client = telegram_client
        self.agent_factory = agent_factory
        self.on_actions = on_actions
        self._agents: dict[str, ChannelAgent] = {}

    def _agent_for_channel(self, channel_id: str) -> ChannelAgent:
        agent = self._agents.get(channel_id)
        if agent is None:
            agent = self.agent_factory(channel_id)
            self._agents[channel_id] = agent
        return agent

    async def start(self, channels: list[str]) -> None:
        async def _handle(message: RawTelegramMessage) -> None:
            agent = self._agent_for_channel(message.channel_id)
            actions = agent.ingest_message(message)
            if self.on_actions is not None:
                maybe = self.on_actions(message.channel_id, actions)
                if inspect.isawaitable(maybe):
                    await maybe

        await self.telegram_client.listen_new_messages(channels, _handle)
