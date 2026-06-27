"""Ajil gateway client wrapper."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from triak_trade.ai.prompts import build_telegram_signal_prompt
from triak_trade.ai.schemas import AIClassificationResult, AIMessageContext

logger = logging.getLogger("triak_trade.ai.gateway_client")
_ARABIC_SCRIPT_RE = re.compile(r"[\u0600-\u06FF]")


class AIGatewayError(Exception):
    """Base AI gateway error."""


class AIGatewayTimeoutError(AIGatewayError):
    """Gateway timeout."""


class AIGatewayHTTPError(AIGatewayError):
    """Gateway returned non-2xx response."""


class AIGatewayResponseError(AIGatewayError):
    """Gateway response malformed/invalid."""


@dataclass(frozen=True, slots=True)
class AIGatewayRoute:
    provider: str
    model: str
    multimodal: bool


@dataclass(slots=True)
class AjilGatewayClient:
    base_url: str
    timeout_seconds: int
    classify_path: str = "/v1/chat/completions"
    auth_header_name: str = "x-api-token"
    auth_token: str = ""
    default_model: str = ""
    provider_priority: tuple[str, ...] = ()
    text_provider: str = "groq"
    text_model: str = "openai/gpt-oss-120b"
    vision_provider: str = "gemini"
    vision_model: str = "gemini-3.1-flash-lite"
    trust_env: bool = False
    retry_attempts: int = 3
    retry_backoff_seconds: float = 0.75
    transport: httpx.BaseTransport | None = None

    def plan_for_context(self, context: AIMessageContext) -> AIGatewayRoute:
        has_images = bool(context.message_images)
        if context.message_has_media or context.message_is_caption or has_images:
            return AIGatewayRoute(
                provider=self.vision_provider,
                model=self.vision_model,
                multimodal=has_images,
            )
        if self._prefer_multilingual_text_route(context.message_text):
            return AIGatewayRoute(
                provider=self.vision_provider,
                model=self.vision_model,
                multimodal=False,
            )
        return AIGatewayRoute(
            provider=self.text_provider,
            model=self.text_model,
            multimodal=False,
        )

    @staticmethod
    def _prefer_multilingual_text_route(message_text: str | None) -> bool:
        if not message_text:
            return False
        return bool(_ARABIC_SCRIPT_RE.search(message_text))

    def classify_message(self, context: AIMessageContext) -> AIClassificationResult:
        last_error: Exception | None = None
        headers = self._build_headers()
        attempts = max(1, self.retry_attempts)
        
        for attempt in range(1, attempts + 1):
            payload = self._build_payload(context, attempt=attempt)
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                    trust_env=self.trust_env,
                    transport=self.transport,
                ) as client:
                    response = client.post(self.classify_path, json=payload, headers=headers)
            except httpx.TimeoutException:
                last_error = AIGatewayTimeoutError("AI gateway timeout")
                logger.warning("AI Gateway timeout on attempt %s/%s", attempt, attempts)
            except httpx.HTTPError as exc:
                last_error = AIGatewayHTTPError(
                    f"AI gateway connection error: {type(exc).__name__}"
                )
                logger.warning(
                    "AI Gateway connection error on attempt %s/%s: %s", attempt, attempts, exc
                )
            else:
                if response.status_code == 429:
                    last_error = AIGatewayHTTPError("AI gateway rate limited (429)")
                    logger.warning("AI Gateway rate limited on attempt %s/%s", attempt, attempts)
                    # Wait longer for rate limits
                    time.sleep(min(30.0, (self.retry_backoff_seconds * 4) * (2 ** (attempt - 1))))
                    continue
                elif response.status_code >= 500:
                    last_error = AIGatewayHTTPError(
                        f"AI gateway server error ({response.status_code})"
                    )
                    logger.warning(
                        "AI Gateway server error %s on attempt %s/%s",
                        response.status_code,
                        attempt,
                        attempts,
                    )
                elif response.status_code >= 400:
                    last_error = AIGatewayHTTPError(
                        f"AI gateway HTTP status {response.status_code}"
                    )
                    # Don't retry 401/403/404 as they are likely permanent
                    if response.status_code in {401, 403, 404}:
                        logger.error(
                            "AI Gateway permanent failure %s on attempt %s/%s: %s",
                            response.status_code,
                            attempt,
                            attempts,
                            response.text,
                        )
                        raise last_error
                    logger.warning(
                        "AI Gateway client error %s on attempt %s/%s",
                        response.status_code,
                        attempt,
                        attempts,
                    )
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        last_error = AIGatewayResponseError(
                            "AI gateway returned malformed JSON"
                        )
                        logger.warning(
                            "AI Gateway returned malformed JSON on attempt %s/%s",
                            attempt,
                            attempts,
                        )
                    else:
                        try:
                            result = self._parse_response_payload(data)
                            if attempt > 1:
                                logger.info("AI Gateway succeeded on attempt %s", attempt)
                            return result
                        except Exception as exc:
                            last_error = AIGatewayResponseError(
                                "AI gateway response schema validation failed: "
                                f"{type(exc).__name__}"
                            )
                            logger.warning(
                                "AI Gateway schema validation failed on attempt %s/%s: %s",
                                attempt,
                                attempts,
                                exc,
                            )

            if attempt < attempts:
                # Exponential backoff with jitter
                base_delay = max(0.1, self.retry_backoff_seconds)
                delay = (base_delay * (2 ** (attempt - 1))) * (0.5 + random.random())
                time.sleep(delay)
        
        if last_error is None:
            raise AIGatewayResponseError("AI gateway classification failed without error detail")
        
        logger.error(f"AI Gateway exhausted all {attempts} attempts. Last error: {last_error}")
        raise last_error

    def _build_payload(self, context: AIMessageContext, *, attempt: int) -> dict[str, Any]:
        prompt = build_telegram_signal_prompt(context)
        context_payload = context.model_dump(mode="json")
        route = self.plan_for_context(context)
        task = "Classify this Telegram channel message and return only valid JSON."
        if attempt > 1:
            task += (
                " Previous attempt was invalid. Return one strict JSON object only, "
                "with every required key present and all price-like fields encoded as strings."
            )
        user_payload = json.dumps(
            {
                "task": task,
                "context": context_payload,
                "required_output_contract": self._output_contract(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        user_content: str | list[dict[str, Any]]
        if route.multimodal and context.message_images:
            content_parts: list[dict[str, Any]] = [{"type": "text", "text": user_payload}]
            for image in context.message_images:
                data_url = image.get("data_url")
                if isinstance(data_url, str) and data_url.startswith("data:image/"):
                    content_parts.append(
                        {"type": "image_url", "image_url": {"url": data_url}}
                    )
            user_content = content_parts
        else:
            user_content = user_payload
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]
        payload: dict[str, Any] = {
            "model": route.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if self.provider_priority:
            router_options: dict[str, Any] = {
                "providers": [route.provider],
                "strategy": "fallback_chain",
                "mode": "quality_first",
                "max_attempts": 2,
                "timeout_sec": max(5.0, float(self.timeout_seconds)),
            }
            router_options["model_preferences"] = [
                {
                    "provider": route.provider,
                    "model": route.model,
                    "priority": 0,
                }
            ]
            payload["x_router"] = router_options
        return payload

    @staticmethod
    def _output_contract() -> dict[str, Any]:
        return {
            "type": "object",
            "json_only": True,
            "required_keys": [
                "classification",
                "action",
                "market",
                "symbol",
                "symbol_raw",
                "side",
                "entry_type",
                "entry_low",
                "entry_high",
                "entry_prices",
                "stop_loss",
                "take_profits",
                "leverage",
                "leverage_mode",
                "close_fraction",
                "move_stop_to_entry",
                "related_signal_id",
                "relation_reason",
                "source_message_ids",
                "extracted_from_context",
                "missing_fields",
                "confidence",
                "reasoning_summary",
                "risk_notes",
                "ignored_numeric_tokens",
                "requires_admin_confirmation",
                "raw_provider_metadata",
            ],
            "enum_fields": {
                "classification": [
                    "NEW_SIGNAL",
                    "SIGNAL_UPDATE",
                    "CANCEL",
                    "CLOSE",
                    "RESULT_REPORT",
                    "ADVERTISEMENT",
                    "GENERAL_ANALYSIS",
                    "UNRELATED",
                    "AMBIGUOUS",
                    "UNKNOWN",
                ],
                "action": [
                    "open",
                    "cancel",
                    "close",
                    "update_sl",
                    "update_tp",
                    "update_leverage",
                    "update_entry",
                    "ignore",
                    "unknown",
                ],
                "market": ["futures", "spot", "unknown"],
                "side": ["long", "short", "buy", "sell", "unknown"],
                "entry_type": ["market", "limit", "range", "unknown"],
                "leverage_mode": ["cross", "isolated", "unknown"],
            },
            "price_string_fields": [
                "entry_low",
                "entry_high",
                "entry_prices",
                "stop_loss",
                "take_profits",
                "confidence",
                "close_fraction",
            ],
        }

    def _build_headers(self) -> dict[str, str]:
        if self.auth_token.strip():
            return {self.auth_header_name: self.auth_token}
        return {}

    def _parse_response_payload(self, data: Any) -> AIClassificationResult:
        if isinstance(data, dict):
            try:
                return AIClassificationResult.model_validate(data)
            except Exception:
                pass

            if "classification" in data:
                try:
                    normalized_direct = self._normalize_result_payload(data)
                    return AIClassificationResult.model_validate(normalized_direct)
                except Exception:
                    pass

            content = self._extract_completion_content(data)
            if content is None:
                raise AIGatewayResponseError(
                    "AI gateway response did not contain classification JSON"
                )
            parsed = self._parse_content_json(content)
            try:
                return AIClassificationResult.model_validate(parsed)
            except Exception:
                normalized = self._normalize_result_payload(parsed)
                return AIClassificationResult.model_validate(normalized)

        raise AIGatewayResponseError("AI gateway response must be a JSON object")

    def _extract_completion_content(self, data: dict[str, Any]) -> str | None:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    text = self._normalize_content(content)
                    if text:
                        return text

        result = data.get("result")
        if isinstance(result, str) and result.strip():
            return result
        if isinstance(result, dict):
            output_text = result.get("output_text")
            if isinstance(output_text, str) and output_text.strip():
                return output_text
        return None

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
                elif isinstance(item, str) and item.strip():
                    parts.append(item)
            return "\n".join(parts)
        return ""

    @staticmethod
    def _parse_content_json(content: str) -> dict[str, Any]:
        stripped = content.strip()
        try:
            parsed = json.loads(stripped)
        except ValueError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise
            parsed = json.loads(stripped[start : end + 1])
        if not isinstance(parsed, dict):
            raise AIGatewayResponseError("AI gateway content JSON must be an object")
        return parsed

    def _normalize_result_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        extracted = payload.get("extracted_fields")
        extracted_fields = extracted if isinstance(extracted, dict) else {}
        classification = self._normalize_classification(payload.get("classification"))
        side = self._normalize_side(
            payload.get("side")
            or payload.get("direction")
            or extracted_fields.get("side")
            or extracted_fields.get("direction")
        )
        leverage = payload.get("leverage") or extracted_fields.get("leverage")
        entry_low = (
            payload.get("entry_low")
            or extracted_fields.get("entry_low")
            or extracted_fields.get("entry_price_min")
            or extracted_fields.get("entry_price")
        )
        entry_high = (
            payload.get("entry_high")
            or extracted_fields.get("entry_high")
            or extracted_fields.get("entry_price_max")
        )
        take_profits = (
            payload.get("take_profits")
            or extracted_fields.get("take_profits")
            or extracted_fields.get("take_profit")
            or extracted_fields.get("targets")
            or []
        )
        market = self._normalize_market(
            payload.get("market"),
            side=side,
            leverage=leverage,
        )
        symbol = self._normalize_symbol(
            payload.get("symbol") or extracted_fields.get("symbol")
        )
        requires_admin_confirmation = bool(
            payload.get(
                "requires_admin_confirmation",
                classification not in {"ADVERTISEMENT", "UNRELATED"},
            )
        )
        return {
            "classification": classification,
            "action": self._normalize_action(payload.get("action"), classification),
            "market": market,
            "symbol": symbol,
            "side": side,
            "entry_type": self._normalize_entry_type(
                payload.get("entry_type"),
                entry_low=entry_low,
                entry_high=entry_high,
            ),
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": payload.get("stop_loss") or extracted_fields.get("stop_loss"),
            "take_profits": self._normalize_take_profit_list(take_profits),
            "leverage": leverage,
            "related_signal_id": payload.get("related_signal_id"),
            "relation_reason": payload.get("relation_reason"),
            "confidence": self._normalize_confidence(
                payload.get("confidence"),
                classification,
            ),
            "reasoning_summary": str(
                payload.get("reasoning_summary")
                or payload.get("summary")
                or "normalized AI gateway response"
            ),
            "risk_notes": self._normalize_string_list(payload.get("risk_notes")),
            "requires_admin_confirmation": requires_admin_confirmation,
            "raw_provider_metadata": self._coerce_metadata(
                payload.get("raw_provider_metadata")
            ),
        }

    @staticmethod
    def _coerce_metadata(raw: Any) -> dict[str, Any]:
        """Coerce provider metadata to a dict without ever raising.

        Some providers return ``raw_provider_metadata`` as a string or list
        instead of a mapping. ``dict("text")`` raises ``ValueError``; guarding
        here keeps a single odd response from failing the whole classification.
        """
        if isinstance(raw, dict):
            return dict(raw)
        if raw is None:
            return {}
        return {"value": raw}

    @staticmethod
    def _normalize_classification(raw: Any) -> str:
        value = str(raw or "UNKNOWN").strip().lower().replace("_", " ")
        mapping = {
            "new signal": "NEW_SIGNAL",
            "signal": "NEW_SIGNAL",
            "signal update": "SIGNAL_UPDATE",
            "update": "SIGNAL_UPDATE",
            "cancel": "CANCEL",
            "cancellation": "CANCEL",
            "close": "CLOSE",
            "result report": "RESULT_REPORT",
            "profit report": "RESULT_REPORT",
            "advertisement": "ADVERTISEMENT",
            "promo": "ADVERTISEMENT",
            "general analysis": "GENERAL_ANALYSIS",
            "analysis": "GENERAL_ANALYSIS",
            "unrelated": "UNRELATED",
            "ambiguous": "AMBIGUOUS",
            "unknown": "UNKNOWN",
        }
        return mapping.get(value, value.upper().replace(" ", "_") or "UNKNOWN")

    @staticmethod
    def _normalize_action(raw: Any, classification: str) -> str:
        if classification in {"AMBIGUOUS", "UNKNOWN"}:
            return "unknown"
        value = str(raw or "").strip().lower()
        if value:
            return value
        mapping = {
            "NEW_SIGNAL": "open",
            "SIGNAL_UPDATE": "unknown",
            "CANCEL": "cancel",
            "CLOSE": "close",
            "RESULT_REPORT": "ignore",
            "ADVERTISEMENT": "ignore",
            "GENERAL_ANALYSIS": "ignore",
            "UNRELATED": "ignore",
            "AMBIGUOUS": "unknown",
            "UNKNOWN": "unknown",
        }
        return mapping.get(classification, "unknown")

    @staticmethod
    def _normalize_symbol(raw: Any) -> str | None:
        value = str(raw or "").strip().upper().replace("/", "").replace("-", "").replace(" ", "")
        return value or None

    @staticmethod
    def _normalize_side(raw: Any) -> str:
        value = str(raw or "unknown").strip().lower()
        mapping = {
            "long": "long",
            "short": "short",
            "buy": "buy",
            "sell": "sell",
        }
        return mapping.get(value, "unknown")

    @staticmethod
    def _normalize_market(raw: Any, *, side: str, leverage: Any) -> str:
        value = str(raw or "").strip().lower()
        if value in {"spot", "futures"}:
            return value
        if side in {"long", "short"} or leverage is not None:
            return "futures"
        if side in {"buy", "sell"}:
            return "spot"
        return "unknown"

    @staticmethod
    def _normalize_entry_type(raw: Any, *, entry_low: Any, entry_high: Any) -> str:
        value = str(raw or "").strip().lower()
        if value in {"market", "limit", "range"}:
            return value
        if entry_low is not None and entry_high is not None and str(entry_low) != str(entry_high):
            return "range"
        if entry_low is not None or entry_high is not None:
            return "limit"
        return "unknown"

    @staticmethod
    def _normalize_take_profit_list(raw: Any) -> list[Any]:
        if isinstance(raw, list):
            normalized: list[Any] = []
            for item in raw:
                if item is None:
                    continue
                if isinstance(item, str):
                    matches = re.findall(r"-?\d+(?:\.\d+)?", item.replace(",", " "))
                    if matches:
                        normalized.extend(matches)
                        continue
                normalized.append(item)
            return normalized
        if raw is None:
            return []
        if isinstance(raw, str):
            matches = re.findall(r"-?\d+(?:\.\d+)?", raw.replace(",", " "))
            if matches:
                return matches
        return [raw]

    @staticmethod
    def _normalize_string_list(raw: Any) -> list[str]:
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        if raw is None:
            return []
        value = str(raw).strip()
        return [value] if value else []

    @staticmethod
    def _normalize_confidence(raw: Any, classification: str) -> str:
        if raw is None:
            return AjilGatewayClient._default_confidence(classification)
        if isinstance(raw, (int, float)):
            return str(raw)
        value = str(raw).strip().lower()
        mapping = {
            "very high": "0.95",
            "high": "0.85",
            "medium": "0.60",
            "moderate": "0.60",
            "low": "0.30",
            "very low": "0.10",
        }
        return mapping.get(value, value or AjilGatewayClient._default_confidence(classification))

    @staticmethod
    def _default_confidence(classification: str) -> str:
        mapping = {
            "NEW_SIGNAL": "0.85",
            "SIGNAL_UPDATE": "0.75",
            "CANCEL": "0.80",
            "CLOSE": "0.80",
            "RESULT_REPORT": "0.90",
            "ADVERTISEMENT": "0.90",
            "GENERAL_ANALYSIS": "0.45",
            "UNRELATED": "0.90",
            "AMBIGUOUS": "0.30",
            "UNKNOWN": "0.20",
        }
        return mapping.get(classification, "0.20")
