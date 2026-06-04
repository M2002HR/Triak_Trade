"""AI classifier implementing MessageClassifier protocol."""

from __future__ import annotations

import re
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
        ai_context = self._build_context(message, context)
        route = self.gateway_client.plan_for_context(ai_context)

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

        parsed = self._repair_with_context(
            self._result_to_parsed_signal(result, message),
            ai_context,
        )
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
                f"ai_route_provider={route.provider}",
                f"ai_route_model={route.model}",
                f"ai_route_multimodal={route.multimodal}",
                f"reply_chain_count={len(ai_context.reply_chain_messages)}",
                f"following_message_count={len(ai_context.following_messages)}",
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

    def _repair_with_context(
        self,
        parsed: ParsedSignal,
        context: AIMessageContext,
    ) -> ParsedSignal:
        text_blocks = [
            block
            for block in [
                context.message_text,
                *[item.get("text") for item in context.reply_chain_messages],
                *[item.get("text") for item in context.following_messages],
            ]
            if isinstance(block, str) and block.strip()
        ]

        extracted_take_profits = self._extract_take_profits(text_blocks)
        extracted_stop_loss = self._extract_stop_loss(text_blocks)
        extracted_leverage = self._extract_leverage(text_blocks)

        take_profits = parsed.take_profits
        if len(extracted_take_profits) > len(take_profits):
            take_profits = extracted_take_profits

        stop_loss = parsed.stop_loss
        if stop_loss is None and extracted_stop_loss is not None:
            stop_loss = extracted_stop_loss

        leverage = parsed.leverage
        if leverage is None and extracted_leverage is not None:
            leverage = extracted_leverage

        if (
            take_profits == parsed.take_profits
            and stop_loss == parsed.stop_loss
            and leverage == parsed.leverage
        ):
            return parsed

        return parsed.model_copy(
            update={
                "take_profits": take_profits,
                "stop_loss": stop_loss,
                "leverage": leverage,
            }
        )

    @staticmethod
    def _extract_take_profits(text_blocks: list[str]) -> list[Decimal]:
        numbers: list[str] = []
        for text in text_blocks:
            lowered = text.lower()
            if not any(keyword in lowered for keyword in ("tp", "target", "targets")):
                continue
            matches = re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", " "))
            numbers.extend(matches)
        deduped = list(dict.fromkeys(numbers))
        return [Decimal(item) for item in deduped]

    @staticmethod
    def _extract_stop_loss(text_blocks: list[str]) -> Decimal | None:
        for text in text_blocks:
            lowered = text.lower()
            if not any(keyword in lowered for keyword in ("sl", "stop", "stoploss")):
                continue
            matches = re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", " "))
            if matches:
                return Decimal(matches[-1])
        return None

    @staticmethod
    def _extract_leverage(text_blocks: list[str]) -> int | None:
        for text in text_blocks:
            lowered = text.lower()
            if "lev" not in lowered and "x" not in lowered:
                continue
            match = re.search(r"(\d{1,3})\s*x", lowered) or re.search(
                r"(?:lev|leverage)\D*(\d{1,3})",
                lowered,
            )
            if match:
                return int(match.group(1))
        return None

    def _build_context(
        self,
        message: RawTelegramMessage,
        context: ChannelContext,
    ) -> AIMessageContext:
        reply_chain = [
            self._serialize_message_context(item)
            for item in context.get_reply_chain(
                message,
                max_depth=self.settings.AI_CLASSIFIER_FORWARD_CONTEXT_LIMIT,
            )
        ]
        following_messages = [
            self._serialize_message_context(item)
            for item in context.get_following_messages(
                message,
                limit=self.settings.AI_CLASSIFIER_FORWARD_CONTEXT_LIMIT,
            )
        ]
        recent_messages = [
            self._serialize_message_context(item)
            for item in context.recent_messages
        ]
        images = self._extract_images(message)
        raw_payload = message.raw_payload
        return AIMessageContext(
            channel_id=message.channel_id,
            channel_username=message.channel_username,
            message_id=message.message_id,
            message_text=message.text,
            message_date=message.date,
            message_has_media=bool(raw_payload.get("has_media")),
            message_is_caption=bool(raw_payload.get("caption_present")),
            message_images=images,
            reply_chain_messages=reply_chain,
            following_messages=following_messages,
            recent_messages=recent_messages,
            active_signals=[
                {
                    "signal_id": signal.signal_id,
                    "status": signal.status.value,
                    "symbol": signal.current_signal.symbol if signal.current_signal else None,
                    "updated_at": signal.updated_at.isoformat(),
                    "related_message_ids": list(signal.related_message_ids),
                }
                for signal in context.active_signals.values()
            ],
            parser_version="ai-v2",
            notes=["ai-classifier", "reply-aware", "forward-context-aware"],
        )

    @staticmethod
    def _serialize_message_context(message: RawTelegramMessage) -> dict[str, object]:
        payload = message.raw_payload
        return {
            "message_id": message.message_id,
            "text": message.text,
            "date": message.date.isoformat(),
            "reply_to": message.reply_to_msg_id,
            "has_media": bool(payload.get("has_media")),
            "caption_present": bool(payload.get("caption_present")),
            "grouped_id": payload.get("grouped_id"),
        }

    @staticmethod
    def _extract_images(message: RawTelegramMessage) -> list[dict[str, object]]:
        raw_images = message.raw_payload.get("image_data_urls")
        if not isinstance(raw_images, list):
            return []
        images: list[dict[str, object]] = []
        for item in raw_images:
            if not isinstance(item, dict):
                continue
            data_url = item.get("data_url")
            mime_type = item.get("mime_type")
            if isinstance(data_url, str) and data_url.startswith("data:image/"):
                images.append(
                    {
                        "mime_type": mime_type if isinstance(mime_type, str) else "image/jpeg",
                        "data_url": data_url,
                    }
                )
        return images

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
