"""Ajil gateway client wrapper."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from triak_trade.ai.prompts import build_telegram_signal_prompt
from triak_trade.ai.schemas import AIClassificationResult, AIMessageContext


class AIGatewayError(Exception):
    """Base AI gateway error."""


class AIGatewayTimeoutError(AIGatewayError):
    """Gateway timeout."""


class AIGatewayHTTPError(AIGatewayError):
    """Gateway returned non-2xx response."""


class AIGatewayResponseError(AIGatewayError):
    """Gateway response malformed/invalid."""


@dataclass(slots=True)
class AjilGatewayClient:
    base_url: str
    timeout_seconds: int
    classify_path: str = "/v1/chat/completions"
    auth_header_name: str = "x-api-token"
    auth_token: str = ""
    default_model: str = ""
    provider_priority: tuple[str, ...] = ()
    trust_env: bool = False
    transport: httpx.BaseTransport | None = None

    def classify_message(self, context: AIMessageContext) -> AIClassificationResult:
        payload = self._build_payload(context)
        headers = self._build_headers()
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                trust_env=self.trust_env,
                transport=self.transport,
            ) as client:
                response = client.post(self.classify_path, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise AIGatewayTimeoutError("AI gateway timeout") from exc
        except httpx.HTTPError as exc:
            raise AIGatewayHTTPError("AI gateway connection error") from exc

        if response.status_code >= 400:
            raise AIGatewayHTTPError(f"AI gateway HTTP status {response.status_code}")

        try:
            data = response.json()
        except ValueError as exc:
            raise AIGatewayResponseError("AI gateway returned malformed JSON") from exc

        try:
            return self._parse_response_payload(data)
        except Exception as exc:
            raise AIGatewayResponseError("AI gateway response schema validation failed") from exc

    def _build_payload(self, context: AIMessageContext) -> dict[str, Any]:
        prompt = build_telegram_signal_prompt(context)
        schema = AIClassificationResult.model_json_schema()
        context_payload = context.model_dump(mode="json")
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": (
                            "Classify this Telegram channel message and return "
                            "only valid JSON."
                        ),
                        "context": context_payload,
                        "required_output_schema": schema,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        ]
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if self.provider_priority:
            router_options: dict[str, Any] = {
                "providers": list(self.provider_priority),
                "strategy": "fallback_chain",
                "mode": "quality_first",
                "max_attempts": max(2, len(self.provider_priority) * 2),
                "timeout_sec": max(5.0, float(self.timeout_seconds)),
            }
            if self.default_model.strip():
                router_options["model_preferences"] = [
                    {
                        "provider": self.provider_priority[0],
                        "model": self.default_model.strip(),
                        "priority": 0,
                    }
                ]
            payload["x_router"] = router_options
        elif self.default_model.strip():
            payload["model"] = self.default_model.strip()
        return payload

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
            "raw_provider_metadata": dict(payload.get("raw_provider_metadata") or {}),
        }

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
            return [item for item in raw if item is not None]
        if raw is None:
            return []
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
