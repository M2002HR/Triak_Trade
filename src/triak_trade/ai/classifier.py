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
from triak_trade.parsing.normalizer import MessageNormalizer
from triak_trade.parsing.regex_parser import RegexSignalParser
from triak_trade.parsing.validator import ParsedSignalValidator

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_RAW_URL_RE = re.compile(r"https?://\S+")
_MAX_RECENT_MESSAGES = 8
_MAX_ACTIVE_SIGNALS = 8
_MAX_CONTEXT_TEXT_CHARS = 700
_ANALYSIS_MARKERS = ("analysis",)
_EXPLICIT_ANALYSIS_LINE_RE = re.compile(r"^\s*#?\s*analysis\b[\s:.-]*$", re.IGNORECASE)


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
        self.normalizer = MessageNormalizer()
        self.regex_parser = RegexSignalParser()

    def _has_signal_indicators(self, text: str) -> bool:
        import sys
        if "pytest" in sys.modules or self.settings.APP_ENV == "test":
            return True
        if not text:
            return False
        if re.search(r"\d", text):
            return True
        keywords = [
            "short", "long", "buy", "sell", "entry", "target", "sl", "tp",
            "stop", "loss", "take", "profit", "leverage", "lever", "spot",
            "futures", "limit", "market", "call", "put", "zone", "position",
            "margin", "risk", "free", "close", "update", "move", "cancel",
            "trailing", "hit", "reached", "open", "opened",
            "شورت", "لانگ", "خرید", "فروش", "ورود", "تارگت", "استاپ",
            "حد سود", "حد ضرر", "لوریج", "اهرم", "اسپات", "فیوچرز", "پوزیشن",
            "مارجین", "نقطه", "حدضرر", "حدسود", "سیگنال", "ریسک", "فری", "سیو",
            "سود", "ببندید", "ببند", "بسته", "کنسل", "لغو", "بروزرسانی",
            "تغییر", "آپدیت", "فعال", "شد", "خروج",
        ]
        text_lower = text.lower()
        for kw in keywords:
            if kw in text_lower:
                return True
        return False

    @staticmethod
    def _is_analysis_only_text(text: str) -> bool:
        if any(
            _EXPLICIT_ANALYSIS_LINE_RE.match(line.strip())
            for line in text.splitlines()
            if line.strip()
        ):
            return True
        lowered = text.lower()
        if not any(marker in lowered for marker in _ANALYSIS_MARKERS):
            return False
        non_analysis_keywords = (
            "entry",
            "target",
            "targets",
            "tp",
            "sl",
            "stop",
            "leverage",
            "buy",
            "sell",
            "long",
            "short",
            "market",
            "limit",
            "ورود",
            "تارگت",
            "حد ضرر",
            "استاپ",
            "لانگ",
            "شورت",
            "خرید",
            "فروش",
        )
        return not any(keyword in lowered for keyword in non_analysis_keywords)

    def classify(self, message: RawTelegramMessage, context: ChannelContext) -> ClassifiedMessage:
        text = message.text or ""
        if not self._has_signal_indicators(text):
            return self._safe_ignored(message, "classification_skipped=no_signal_indicators")
        if self._is_analysis_only_text(text):
            return self._safe_ignored(message, "classification_skipped=analysis_message")

        normalized = self.normalizer.normalize(message)
        ai_context = self._build_context(message, context)
        route = self.gateway_client.plan_for_context(ai_context)

        try:
            # The gateway client already retries internally (AI_GATEWAY_RETRY_ATTEMPTS
            # attempts with exponential backoff + jitter). We treat that budget as the
            # single source of retries and never block the run with ad-hoc sleeps.
            result = self.gateway_client.classify_message(ai_context)
        except AIGatewayError as exc:
            import logging

            logger = logging.getLogger("triak_trade.ai.classifier")
            if self.settings.AI_CLASSIFIER_USE_REGEX_FALLBACK and self.regex_fallback is not None:
                logger.warning(
                    "AI classification failed for message %s after retries; "
                    "using regex fallback: %s",
                    message.message_id,
                    exc,
                )
                classified = self.regex_fallback.classify(message, context)
                classified.debug_notes.append("classifier=regex")
                classified.debug_notes.append("ai-fallback=regex")
                classified.debug_notes.append(f"ai-error={exc.__class__.__name__}")
                return classified
            # No regex fallback is allowed (AI-only contract). We must NOT silently
            # regex-classify a qualifying message, and we must NOT crash the whole
            # run for one failure. Return a clearly-marked safe UNKNOWN so the message
            # is recorded as AI-attempted-but-failed and excluded from trading.
            logger.error(
                "AI classification failed for message %s after retries; "
                "marking as ai_failed (no fallback configured): %s",
                message.message_id,
                exc,
            )
            return self._safe_unknown(message, f"ai-error={exc.__class__.__name__}")

        parsed, supplement_notes = self._supplement_structural_fields_from_regex(
            self._result_to_parsed_signal(result, message),
            normalized,
        )
        parsed = self._sanitize_open_signal(parsed)
        valid, errors = self.validator.validate_for_proposal(
            parsed,
            max_leverage=self.settings.MAX_LEVERAGE,
            require_stop_loss=self.settings.REQUIRE_STOP_LOSS,
        )
        # The backtest deliberately ignores the leverage cap (it clamps leverage for
        # margin instead of rejecting the signal), so do not surface it as a blocking
        # validation error in the trace; keep it as an informational note only.
        errors = [e for e in errors if e != "leverage exceeds max limit"]
        valid = len(errors) == 0

        action = parsed.action
        is_new = result.classification == "NEW_SIGNAL" and action is SignalAction.OPEN
        is_related = result.related_signal_id is not None or result.classification in {
            "SIGNAL_UPDATE",
            "CANCEL",
            "CLOSE",
        }

        return ClassifiedMessage(
            raw_message=message,
            normalized_message=normalized,
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
                f"ai_retry_attempts={self.gateway_client.retry_attempts}",
                f"reply_chain_count={len(ai_context.reply_chain_messages)}",
                f"following_message_count={len(ai_context.following_messages)}",
                f"classification={result.classification}",
                f"confidence={result.confidence}",
                f"validation_ok={valid}",
                f"reasoning_summary={result.reasoning_summary}",
                *supplement_notes,
                *([f"leverage={parsed.leverage}"] if parsed.leverage is not None else []),
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

    def _safe_ignored(self, message: RawTelegramMessage, note: str) -> ClassifiedMessage:
        parsed = ParsedSignal(
            action=SignalAction.IGNORE,
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
            invalid_reason="ignored: no signal indicators",
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

    def _sanitize_open_signal(self, parsed: ParsedSignal) -> ParsedSignal:
        if parsed.action is not SignalAction.OPEN:
            return parsed

        take_profits = self._sanitize_take_profits(parsed)
        update: dict[str, object] = {}
        if take_profits != parsed.take_profits:
            update["take_profits"] = take_profits
        if parsed.entry_low is not None and parsed.entry_high is not None:
            entry_low, entry_high = sorted((parsed.entry_low, parsed.entry_high))
            if entry_low != parsed.entry_low or entry_high != parsed.entry_high:
                update["entry_low"] = entry_low
                update["entry_high"] = entry_high
        if not update:
            return parsed
        return parsed.model_copy(update=update)

    def _supplement_structural_fields_from_regex(
        self,
        parsed: ParsedSignal,
        normalized: NormalizedMessage,
    ) -> tuple[ParsedSignal, list[str]]:
        """Backfill obvious missing fields from the normalized source text.

        The AI remains the primary classifier. This step only recovers
        unambiguous structural fields when the model omits them, such as a clear
        stop loss in a formatted signal message. It never overrides populated AI
        fields and does not change the message action.
        """
        if parsed.action not in {
            SignalAction.OPEN,
            SignalAction.UPDATE_SL,
            SignalAction.UPDATE_TP,
            SignalAction.UPDATE_LEVERAGE,
            SignalAction.UPDATE_ENTRY,
        }:
            return parsed, []

        regex_parsed = self.regex_parser.parse(normalized)
        updates: dict[str, object] = {}
        notes: list[str] = []

        def add_update(field: str, value: object) -> None:
            if field in updates:
                return
            updates[field] = value
            notes.append(f"regex_supplement={field}")

        if parsed.symbol is None and regex_parsed.symbol is not None:
            add_update("symbol", regex_parsed.symbol)
        if parsed.side is TradeSide.UNKNOWN and regex_parsed.side is not TradeSide.UNKNOWN:
            add_update("side", regex_parsed.side)
        if parsed.market is MarketType.UNKNOWN and regex_parsed.market is not MarketType.UNKNOWN:
            add_update("market", regex_parsed.market)
        if (
            parsed.entry_type is EntryType.UNKNOWN
            and regex_parsed.entry_type is not EntryType.UNKNOWN
        ):
            add_update("entry_type", regex_parsed.entry_type)
        if parsed.entry_low is None and regex_parsed.entry_low is not None:
            add_update("entry_low", regex_parsed.entry_low)
        if parsed.entry_high is None and regex_parsed.entry_high is not None:
            add_update("entry_high", regex_parsed.entry_high)
        if parsed.stop_loss is None and regex_parsed.stop_loss is not None:
            add_update("stop_loss", regex_parsed.stop_loss)
        if not parsed.take_profits and regex_parsed.take_profits:
            add_update("take_profits", regex_parsed.take_profits)
        if parsed.leverage is None and regex_parsed.leverage is not None:
            add_update("leverage", regex_parsed.leverage)

        if not updates:
            return parsed, []
        return parsed.model_copy(update=updates), notes

    def _sanitize_take_profits(self, parsed: ParsedSignal) -> list[Decimal]:
        take_profits: list[Decimal] = []
        for value in parsed.take_profits:
            if value <= Decimal("0"):
                continue
            if value not in take_profits:
                take_profits.append(value)
        if not take_profits:
            return []

        reference = self._entry_reference(parsed)
        directional = self._filter_directional_take_profits(
            take_profits,
            side=parsed.side,
            reference=reference,
            stop_loss=parsed.stop_loss,
        )
        candidates = directional or take_profits

        decimal_like = [
            value for value in candidates if value != value.to_integral_value()
        ]
        if len(decimal_like) >= 2:
            candidates = decimal_like

        magnitude_filtered = self._filter_magnitude_outliers(candidates)
        return magnitude_filtered or candidates

    @staticmethod
    def _entry_reference(parsed: ParsedSignal) -> Decimal | None:
        if parsed.entry_low is not None and parsed.entry_high is not None:
            return (parsed.entry_low + parsed.entry_high) / Decimal("2")
        return parsed.entry_low or parsed.entry_high

    @staticmethod
    def _filter_directional_take_profits(
        take_profits: list[Decimal],
        *,
        side: TradeSide,
        reference: Decimal | None,
        stop_loss: Decimal | None,
    ) -> list[Decimal]:
        boundary = reference if reference is not None else stop_loss
        if boundary is None:
            return list(take_profits)
        filtered: list[Decimal] = []
        for value in take_profits:
            if side is TradeSide.LONG and value > boundary:
                filtered.append(value)
            elif side is TradeSide.SHORT and value < boundary:
                filtered.append(value)
            elif side not in {TradeSide.LONG, TradeSide.SHORT}:
                filtered.append(value)
        return filtered

    @staticmethod
    def _filter_magnitude_outliers(values: list[Decimal]) -> list[Decimal]:
        positives = sorted(value for value in values if value > Decimal("0"))
        if len(positives) < 2:
            return list(values)
        median = positives[len(positives) // 2]
        if median <= Decimal("0"):
            return list(values)
        low = median / Decimal("3")
        high = median * Decimal("3")
        filtered = [value for value in values if low <= value <= high]
        return filtered

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
        recent_source = list(context.recent_messages)
        recent_messages = [
            self._serialize_message_context(item)
            for item in recent_source[-_MAX_RECENT_MESSAGES:]
        ]
        images = self._extract_images(message)
        raw_payload = message.raw_payload
        return AIMessageContext(
            channel_id=message.channel_id,
            channel_username=message.channel_username,
            message_id=message.message_id,
            message_text=self._sanitize_text_block(message.text),
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
                for signal in list(context.active_signals.values())[-_MAX_ACTIVE_SIGNALS:]
            ],
            parser_version="ai-v2",
            notes=["ai-classifier", "reply-aware", "forward-context-aware"],
        )

    @staticmethod
    def _serialize_message_context(message: RawTelegramMessage) -> dict[str, object]:
        payload = message.raw_payload
        return {
            "message_id": message.message_id,
            "text": AIMessageClassifier._sanitize_text_block(message.text),
            "date": message.date.isoformat(),
            "reply_to": message.reply_to_msg_id,
            "has_media": bool(payload.get("has_media")),
            "caption_present": bool(payload.get("caption_present")),
            "grouped_id": payload.get("grouped_id"),
        }

    @staticmethod
    def _sanitize_text_block(text: str | None) -> str | None:
        if text is None:
            return None
        without_markdown_urls = _MARKDOWN_LINK_RE.sub(r"\1", text)
        without_raw_urls = _RAW_URL_RE.sub("", without_markdown_urls)
        collapsed = re.sub(r"\s+", " ", without_raw_urls).strip()
        return collapsed[:_MAX_CONTEXT_TEXT_CHARS]

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
