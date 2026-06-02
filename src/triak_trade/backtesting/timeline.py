"""Timeline reconstruction from messages."""

from __future__ import annotations

from triak_trade.agents.classifier import MessageClassifier
from triak_trade.agents.context import ChannelContext
from triak_trade.backtesting.models import BacktestEvent
from triak_trade.domain.ids import make_signal_id
from triak_trade.domain.models import RawTelegramMessage


class BacktestTimelineBuilder:
    def __init__(self, *, classifier: MessageClassifier, channel_id: str) -> None:
        self.classifier = classifier
        self.context = ChannelContext(
            channel_id=channel_id,
            max_message_limit=5000,
            max_update_window_hours=48,
        )

    def build(self, messages: list[RawTelegramMessage]) -> list[BacktestEvent]:
        sorted_msgs = sorted(messages, key=lambda item: item.date)
        events: list[BacktestEvent] = []
        for message in sorted_msgs:
            self.context.add_recent_message(message)
            classified = self.classifier.classify(message, self.context)
            signal_id = None
            if classified.is_potential_new_signal:
                signal_id = make_signal_id(message.channel_id, message.message_id)
            elif classified.related_signal_id is not None:
                signal_id = classified.related_signal_id

            events.append(
                BacktestEvent(
                    timestamp=message.date,
                    action=classified.parsed_signal.action,
                    signal_id=signal_id,
                    parsed_signal=classified.parsed_signal,
                    related_signal_id=classified.related_signal_id,
                    debug_notes=classified.debug_notes,
                )
            )
        return events
