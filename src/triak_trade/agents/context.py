"""In-memory per-channel context."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from triak_trade.domain.models import ParsedSignal, RawTelegramMessage, SignalState


class ChannelContext:
    def __init__(
        self,
        *,
        channel_id: str,
        max_message_limit: int,
        max_update_window_hours: int,
    ) -> None:
        self.channel_id = channel_id
        self.max_message_limit = max_message_limit
        self.max_update_window_hours = max_update_window_hours
        self.recent_messages: deque[RawTelegramMessage] = deque(maxlen=max_message_limit)
        self.recent_message_ids: deque[int] = deque(maxlen=max_message_limit)
        self.message_by_id: dict[int, RawTelegramMessage] = {}
        self.active_signals: dict[str, SignalState] = {}
        self.pending_signal_ids: set[str] = set()
        self.signal_by_message_id: dict[int, str] = {}
        self.catalog_message_ids: list[int] = []

    def add_recent_message(self, message: RawTelegramMessage) -> None:
        if len(self.recent_message_ids) == self.max_message_limit:
            oldest_id = self.recent_message_ids[0]
            self.message_by_id.pop(oldest_id, None)
        self.recent_messages.append(message)
        self.recent_message_ids.append(message.message_id)
        self.message_by_id[message.message_id] = message

    def seed_message_catalog(self, messages: list[RawTelegramMessage]) -> None:
        self.catalog_message_ids = [message.message_id for message in messages]
        for message in messages:
            self.message_by_id[message.message_id] = message

    def add_signal(self, signal: SignalState, *, pending: bool) -> None:
        self.active_signals[signal.signal_id] = signal
        for message_id in signal.related_message_ids:
            self.signal_by_message_id[message_id] = signal.signal_id
        if pending:
            self.pending_signal_ids.add(signal.signal_id)
        else:
            self.pending_signal_ids.discard(signal.signal_id)

    def get_signal(self, signal_id: str) -> SignalState | None:
        return self.active_signals.get(signal_id)

    def is_within_update_window(self, signal: SignalState, now: datetime) -> bool:
        return signal.updated_at + timedelta(hours=self.max_update_window_hours) >= now

    def find_signal_by_message_reply(self, reply_to_msg_id: int | None) -> SignalState | None:
        if reply_to_msg_id is None:
            return None
        signal_id = self.signal_by_message_id.get(reply_to_msg_id)
        if signal_id is None:
            return None
        return self.active_signals.get(signal_id)

    def find_signals_by_symbol(self, symbol: str | None) -> list[SignalState]:
        if symbol is None:
            return []
        return [
            signal
            for signal in self.active_signals.values()
            if signal.current_signal is not None and signal.current_signal.symbol == symbol
        ]

    def get_message(self, message_id: int | None) -> RawTelegramMessage | None:
        if message_id is None:
            return None
        return self.message_by_id.get(message_id)

    def get_reply_chain(
        self,
        message: RawTelegramMessage,
        *,
        max_depth: int = 3,
    ) -> list[RawTelegramMessage]:
        chain: list[RawTelegramMessage] = []
        current_reply = message.reply_to_msg_id
        while current_reply is not None and len(chain) < max_depth:
            parent = self.get_message(current_reply)
            if parent is None:
                break
            chain.append(parent)
            current_reply = parent.reply_to_msg_id
        return chain

    def get_following_messages(
        self,
        message: RawTelegramMessage,
        *,
        limit: int,
    ) -> list[RawTelegramMessage]:
        if not self.catalog_message_ids:
            return []
        try:
            index = self.catalog_message_ids.index(message.message_id)
        except ValueError:
            return []
        following_ids = self.catalog_message_ids[index + 1 : index + 1 + limit]
        return [
            self.message_by_id[msg_id]
            for msg_id in following_ids
            if msg_id in self.message_by_id
        ]

    def find_recent_pending_signals(
        self,
        *,
        before_message_id: int,
        limit_messages: int,
    ) -> list[SignalState]:
        recent_ids = list(self.recent_message_ids)
        try:
            before_index = recent_ids.index(before_message_id)
        except ValueError:
            before_index = len(recent_ids)
        candidate_ids = recent_ids[max(0, before_index - limit_messages) : before_index]
        signal_ids = {
            self.signal_by_message_id[msg_id]
            for msg_id in candidate_ids
            if msg_id in self.signal_by_message_id
        }
        return [
            self.active_signals[signal_id]
            for signal_id in signal_ids
            if signal_id in self.pending_signal_ids
        ]

    def attach_message(self, signal_id: str, message: RawTelegramMessage) -> None:
        signal = self.active_signals.get(signal_id)
        if signal is None:
            return
        if message.message_id not in signal.related_message_ids:
            signal.related_message_ids.append(message.message_id)
        signal.updated_at = message.date
        self.signal_by_message_id[message.message_id] = signal_id

    def merge_signal(self, signal_id: str, parsed: ParsedSignal, updated_at: datetime) -> None:
        signal = self.active_signals.get(signal_id)
        if signal is None:
            return
        current = signal.current_signal
        if current is None:
            signal.current_signal = parsed
        else:
            signal.current_signal = ParsedSignal(
                action=parsed.action if parsed.action.value != "unknown" else current.action,
                market=parsed.market if parsed.market.value != "unknown" else current.market,
                symbol=parsed.symbol or current.symbol,
                side=parsed.side if parsed.side.value != "unknown" else current.side,
                entry_type=(
                    parsed.entry_type
                    if parsed.entry_type.value != "unknown"
                    else current.entry_type
                ),
                entry_low=parsed.entry_low if parsed.entry_low is not None else current.entry_low,
                entry_high=(
                    parsed.entry_high
                    if parsed.entry_high is not None
                    else current.entry_high
                ),
                stop_loss=parsed.stop_loss if parsed.stop_loss is not None else current.stop_loss,
                take_profits=parsed.take_profits or current.take_profits,
                leverage=parsed.leverage if parsed.leverage is not None else current.leverage,
                confidence=(
                    parsed.confidence
                    if parsed.confidence > current.confidence
                    else current.confidence
                ),
                invalid_reason=parsed.invalid_reason or current.invalid_reason,
                source_channel_id=current.source_channel_id,
                source_message_id=current.source_message_id,
                parser_version=parsed.parser_version,
            )
        signal.updated_at = updated_at
        signal.version += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "channel_id": self.channel_id,
            "recent_message_ids": [m.message_id for m in self.recent_messages],
            "signals": {
                signal_id: {
                    "status": state.status.value,
                    "related_message_ids": list(state.related_message_ids),
                    "symbol": state.current_signal.symbol if state.current_signal else None,
                }
                for signal_id, state in self.active_signals.items()
            },
        }
