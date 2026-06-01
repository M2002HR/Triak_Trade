"""Ajil gateway client wrapper."""

from __future__ import annotations

from dataclasses import dataclass

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
    classify_path: str = "/v1/classify/telegram-signal"
    transport: httpx.BaseTransport | None = None

    def classify_message(self, context: AIMessageContext) -> AIClassificationResult:
        payload = {
            "prompt": build_telegram_signal_prompt(context),
            "context": context.model_dump(mode="json"),
        }
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.post(self.classify_path, json=payload)
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
            return AIClassificationResult.model_validate(data)
        except Exception as exc:
            raise AIGatewayResponseError("AI gateway response schema validation failed") from exc
