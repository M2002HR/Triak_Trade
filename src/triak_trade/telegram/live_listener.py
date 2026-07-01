"""Live message listener service."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable

from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.core.logging import log_event, safe_preview
from triak_trade.domain.models import ProposedAction, RawTelegramMessage
from triak_trade.telegram.client import TelegramClientInterface

_log = logging.getLogger(__name__)


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
            log_event(
                _log,
                logging.INFO,
                "telegram_live_listener.agent_created",
                channel_id=channel_id,
                agent_type=agent.__class__.__name__,
            )
        return agent

    async def start(self, channels: list[str]) -> None:
        log_event(
            _log,
            logging.INFO,
            "telegram_live_listener.started",
            channel_count=len(channels),
            channels=channels,
        )

        async def _handle(message: RawTelegramMessage) -> None:
            payload = message.raw_payload
            log_event(
                _log,
                logging.DEBUG,
                "telegram_live_listener.message_received",
                channel_id=message.channel_id,
                message_id=message.message_id,
                has_media=bool(payload.get("has_media")),
                caption_present=bool(payload.get("caption_present")),
                preview=safe_preview(message.text),
            )
            if bool(payload.get("has_media")) and bool(payload.get("caption_present")):
                message = await self.telegram_client.ensure_media_payload(message)
                log_event(
                    _log,
                    logging.DEBUG,
                    "telegram_live_listener.media_payload_ensured",
                    channel_id=message.channel_id,
                    message_id=message.message_id,
                    image_count=len(message.raw_payload.get("image_data_urls", [])),
                )
            agent = self._agent_for_channel(message.channel_id)
            actions = agent.ingest_message(message)
            log_event(
                _log,
                logging.DEBUG,
                "telegram_live_listener.actions_generated",
                channel_id=message.channel_id,
                message_id=message.message_id,
                action_count=len(actions),
            )
            if self.on_actions is not None:
                maybe = self.on_actions(message.channel_id, actions)
                if inspect.isawaitable(maybe):
                    await maybe
                log_event(
                    _log,
                    logging.DEBUG,
                    "telegram_live_listener.actions_dispatched",
                    channel_id=message.channel_id,
                    message_id=message.message_id,
                    action_count=len(actions),
                )

        await self.telegram_client.listen_new_messages(channels, _handle)
