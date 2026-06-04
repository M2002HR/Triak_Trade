"""AI classifier implementing MessageClassifier protocol."""

from __future__ import annotations

from decimal import Decimal

from triak_trade.agents.classifier import (
    ClassifiedMessage,
    MessageClassifier,
)
from triak_trade.agents.context import ChannelContext
from triak_trade.ai.gateway_client import AIGatewayError, AjilGatewayClient
from triak_trade.ai.schemas import AIClassificationResult, AIMessageContext
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import EntryType, MarketType, SignalAction, TradeSide
from triak_trade.domain.models import NormalizedMessage, ParsedSignal, RawTelegramMessage
from triak_trade.parsing.validator import ParsedSignalValidator


class AIMessageClassifier(MessageClassifier):
    def __init__(
        self,
        *,
        settings: Settings,
        gateway_client: AjilGatewayClient,
        regex_fallback: MessageClassifier | None = None,
    ) -> None:
        self.settings = settings
        self.gateway_client = gateway_client
        self.regex_fallback = regex_fallback
        self.validator = ParsedSignalValidator()

    def classify(self, message: RawTelegramMessage, context: ChannelContext) -> ClassifiedMessage:
        ai_context = AIMessageContext(
            channel_id=message.channel_id,
            channel_username=message.channel_username,
            message_id=message.message_id,
            message_text=message.text,
            message_date=message.date,
            recent_messages=[
                {
                    "message_id": m.message_id,
                    "text": m.text,
                    "date": m.date.isoformat(),
                    "reply_to": m.reply_to_msg_id,
                }
                for m in context.recent_messages
            ],
            active_signals=[
                {
                    "signal_id": signal.signal_id,
                    "status": signal.status.value,
                    "symbol": signal.current_signal.symbol if signal.current_signal else None,
                    "updated_at": signal.updated_at.isoformat(),
                }
                for signal in context.active_signals.values()
            ],
            parser_version="ai-v1",
            notes=["ai-classifier"],
        )

        try:
            result = self.gateway_client.classify_message(ai_context)
        except AIGatewayError as exc:
            if self.settings.AI_CLASSIFIER_USE_REGEX_FALLBACK and self.regex_fallback is not None:
                classified = self.regex_fallback.classify(message, context)
                classified.debug_notes.append("classifier=regex")
                classified.debug_notes.append("ai-fallback=regex")
                classified.debug_notes.append(f"ai-error={exc.__class__.__name__}")
                return classified
            return self._safe_unknown(message, f"ai-failed:{exc.__class__.__name__}")

        parsed = self._result_to_parsed_signal(result, message)
        valid, errors = self.validator.validate_for_proposal(
            parsed,
            max_leverage=self.settings.MAX_LEVERAGE,
            require_stop_loss=self.settings.REQUIRE_STOP_LOSS,
        )

        action = parsed.action
        is_new = result.classification == "NEW_SIGNAL" and action is SignalAction.OPEN
        is_related = result.related_signal_id is not None or result.classification in {
            "SIGNAL_UPDATE",
            "CANCEL",
            "CLOSE",
        }

        return ClassifiedMessage(
            raw_message=message,
            normalized_message=NormalizedMessage(
                raw=message,
                normalized_text=message.text or "",
                detected_symbols=[parsed.symbol] if parsed.symbol else [],
                detected_keywords=[],
                language_hint=None,
            ),
            parsed_signal=parsed,
            is_potential_new_signal=is_new,
            is_related_to_existing_signal=is_related,
            related_signal_id=result.related_signal_id,
            relation_reason=result.relation_reason,
            confidence=result.confidence,
            debug_notes=[
                "classifier=ai",
                f"ai_gateway_path={self.gateway_client.classify_path}",
                f"classification={result.classification}",
                f"confidence={result.confidence}",
                f"validation_ok={valid}",
                f"reasoning_summary={result.reasoning_summary}",
                *[f"validation_error={e}" for e in errors],
            ],
        )

    def _result_to_parsed_signal(
        self,
        result: AIClassificationResult,
        message: RawTelegramMessage,
    ) -> ParsedSignal:
        action = self._map_action(result)
        market = self._map_market(result.market)
        side = self._map_side(result.side)
        entry_type = self._map_entry_type(result.entry_type)

        return ParsedSignal(
            action=action,
            market=market,
            symbol=result.symbol,
            side=side,
            entry_type=entry_type,
            entry_low=result.entry_low,
            entry_high=result.entry_high,
            stop_loss=result.stop_loss,
            take_profits=result.take_profits,
            leverage=result.leverage,
            confidence=result.confidence,
            invalid_reason=None if action is not SignalAction.UNKNOWN else "ai-ambiguous",
            source_channel_id=message.channel_id,
            source_message_id=message.message_id,
            parser_version="ai-v1",
        )

    def _safe_unknown(self, message: RawTelegramMessage, note: str) -> ClassifiedMessage:
        parsed = ParsedSignal(
            action=SignalAction.UNKNOWN,
            market=MarketType.UNKNOWN,
            symbol=None,
            side=TradeSide.UNKNOWN,
            entry_type=EntryType.UNKNOWN,
            entry_low=None,
            entry_high=None,
            stop_loss=None,
            take_profits=[],
            leverage=None,
            confidence=Decimal("0.10"),
            invalid_reason="ai unavailable",
            source_channel_id=message.channel_id,
            source_message_id=message.message_id,
            parser_version="ai-v1",
        )
        return ClassifiedMessage(
            raw_message=message,
            normalized_message=None,
            parsed_signal=parsed,
            is_potential_new_signal=False,
            is_related_to_existing_signal=False,
            related_signal_id=None,
            relation_reason=None,
            confidence=Decimal("0.10"),
            debug_notes=["classifier=ai", note],
        )

    @staticmethod
    def _map_action(result: AIClassificationResult) -> SignalAction:
        action_raw = result.action.lower().strip()
        if result.classification == "NEW_SIGNAL" and action_raw == "ignore":
            return SignalAction.UNKNOWN

        by_classification = {
            "NEW_SIGNAL": SignalAction.OPEN,
            "CANCEL": SignalAction.CANCEL,
            "CLOSE": SignalAction.CLOSE,
            "RESULT_REPORT": SignalAction.IGNORE,
            "ADVERTISEMENT": SignalAction.IGNORE,
            "GENERAL_ANALYSIS": SignalAction.IGNORE,
            "UNRELATED": SignalAction.IGNORE,
            "AMBIGUOUS": SignalAction.UNKNOWN,
            "UNKNOWN": SignalAction.UNKNOWN,
        }
        if result.classification in by_classification:
            return by_classification[result.classification]

        by_action = {
            "open": SignalAction.OPEN,
            "cancel": SignalAction.CANCEL,
            "close": SignalAction.CLOSE,
            "update_sl": SignalAction.UPDATE_SL,
            "update_tp": SignalAction.UPDATE_TP,
            "update_leverage": SignalAction.UPDATE_LEVERAGE,
            "ignore": SignalAction.IGNORE,
            "unknown": SignalAction.UNKNOWN,
        }
        return by_action.get(action_raw, SignalAction.UNKNOWN)

    @staticmethod
    def _map_market(raw: str) -> MarketType:
        value = raw.lower()
        if value == "spot":
            return MarketType.SPOT
        if value == "futures":
            return MarketType.FUTURES
        return MarketType.UNKNOWN

    @staticmethod
    def _map_side(raw: str) -> TradeSide:
        value = raw.lower()
        mapping = {
            "long": TradeSide.LONG,
            "short": TradeSide.SHORT,
            "buy": TradeSide.BUY,
            "sell": TradeSide.SELL,
        }
        return mapping.get(value, TradeSide.UNKNOWN)

    @staticmethod
    def _map_entry_type(raw: str) -> EntryType:
        value = raw.lower()
        mapping = {
            "market": EntryType.MARKET,
            "limit": EntryType.LIMIT,
            "range": EntryType.RANGE,
        }
        return mapping.get(value, EntryType.UNKNOWN)
