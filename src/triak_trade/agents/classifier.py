"""Message classifier protocol and regex adapter."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field

from triak_trade.agents.context import ChannelContext
from triak_trade.domain.enums import SignalAction
from triak_trade.domain.models import NormalizedMessage, ParsedSignal, RawTelegramMessage
from triak_trade.parsing.normalizer import MessageNormalizer
from triak_trade.parsing.regex_parser import RegexSignalParser


class ClassifiedMessage(BaseModel):
    raw_message: RawTelegramMessage
    normalized_message: NormalizedMessage | None
    parsed_signal: ParsedSignal
    is_potential_new_signal: bool
    is_related_to_existing_signal: bool
    related_signal_id: str | None
    relation_reason: str | None
    confidence: Decimal
    debug_notes: list[str] = Field(default_factory=list)


class MessageClassifier(Protocol):
    def classify(self, message: RawTelegramMessage, context: ChannelContext) -> ClassifiedMessage:
        ...


class RegexMessageClassifier:
    def __init__(self) -> None:
        self.normalizer = MessageNormalizer()
        self.parser = RegexSignalParser()

    def classify(self, message: RawTelegramMessage, context: ChannelContext) -> ClassifiedMessage:
        normalized = self.normalizer.normalize(message)
        parsed = self.parser.parse(normalized)

        relation_reason: str | None = None
        related_signal_id: str | None = None
        debug_notes: list[str] = []

        by_reply = context.find_signal_by_message_reply(message.reply_to_msg_id)
        if by_reply is not None:
            related_signal_id = by_reply.signal_id
            relation_reason = "reply_to_msg_id"
            debug_notes.append("linked by reply")

        if related_signal_id is None and parsed.symbol is not None:
            same_symbol = context.find_signals_by_symbol(parsed.symbol)
            if len(same_symbol) == 1:
                related_signal_id = same_symbol[0].signal_id
                relation_reason = "same_symbol"
                debug_notes.append("linked by symbol")
            elif len(same_symbol) > 1:
                relation_reason = "ambiguous_same_symbol"
                debug_notes.append("ambiguous symbol relation")

        update_like = parsed.action in {
            SignalAction.UPDATE_SL,
            SignalAction.UPDATE_TP,
            SignalAction.UPDATE_LEVERAGE,
            SignalAction.CANCEL,
            SignalAction.CLOSE,
        }

        is_new = parsed.action is SignalAction.OPEN and related_signal_id is None
        is_related = related_signal_id is not None or update_like

        return ClassifiedMessage(
            raw_message=message,
            normalized_message=normalized,
            parsed_signal=parsed,
            is_potential_new_signal=is_new,
            is_related_to_existing_signal=is_related,
            related_signal_id=related_signal_id,
            relation_reason=relation_reason,
            confidence=parsed.confidence,
            debug_notes=debug_notes,
        )
