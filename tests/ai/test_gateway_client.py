from __future__ import annotations

import os

import httpx
import pytest

from triak_trade.ai.gateway_client import (
    AIGatewayHTTPError,
    AIGatewayResponseError,
    AIGatewayTimeoutError,
    AjilGatewayClient,
)
from triak_trade.ai.schemas import AIMessageContext


@pytest.fixture
def context() -> AIMessageContext:
    return AIMessageContext(
        channel_id="c1",
        channel_username="u1",
        message_id=1,
        message_text="BTCUSDT LONG",
        message_date="2026-01-01T00:00:00Z",
        recent_messages=[],
        active_signals=[],
        parser_version="ai-v1",
        notes=[],
    )


def _ok_payload() -> dict[str, object]:
    return {
        "classification": "NEW_SIGNAL",
        "action": "open",
        "market": "futures",
        "symbol": "BTCUSDT",
        "side": "long",
        "entry_type": "range",
        "entry_low": "68000",
        "entry_high": "68200",
        "stop_loss": "67400",
        "take_profits": ["69000", "70000"],
        "leverage": 5,
        "related_signal_id": None,
        "relation_reason": None,
        "confidence": "0.90",
        "reasoning_summary": "clear signal",
        "risk_notes": [],
        "requires_admin_confirmation": True,
        "raw_provider_metadata": {"provider": "mock"},
    }


def test_gateway_client_sends_payload(context: AIMessageContext) -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["auth"] = request.headers.get("x-api-token")
        observed["json"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": httpx.Response(200, json=_ok_payload()).text,
                        }
                    }
                ]
            },
        )

    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        classify_path="/v1/chat/completions",
        auth_token="test-token",
        default_model="gemini-2.5-flash",
        provider_priority=("gemini", "groq"),
        transport=httpx.MockTransport(handler),
    )
    result = client.classify_message(context)
    assert result.classification == "NEW_SIGNAL"
    assert observed["path"] == "/v1/chat/completions"
    assert observed["auth"] == "test-token"
    assert "messages" in str(observed["json"])
    assert "response_format" in str(observed["json"])
    assert "x_router" in str(observed["json"])


def test_gateway_client_timeout(context: AIMessageContext) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AIGatewayTimeoutError):
        client.classify_message(context)


def test_gateway_client_non_2xx(context: AIMessageContext) -> None:
    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"error": "x"})),
    )
    with pytest.raises(AIGatewayHTTPError):
        client.classify_message(context)


def test_gateway_client_malformed_json(context: AIMessageContext) -> None:
    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="not-json")),
    )
    with pytest.raises(AIGatewayResponseError):
        client.classify_message(context)


def test_gateway_client_schema_invalid(context: AIMessageContext) -> None:
    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"classification": "NEW_SIGNAL"})
        ),
    )
    with pytest.raises(AIGatewayResponseError):
        client.classify_message(context)


def test_gateway_client_accepts_direct_schema_payload(context: AIMessageContext) -> None:
    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_ok_payload())),
    )
    result = client.classify_message(context)
    assert result.symbol == "BTCUSDT"


def test_gateway_client_normalizes_alternate_ai_schema(context: AIMessageContext) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": """```json
                    {
                      "classification": "new signal",
                      "reasoning_summary": "clear signal",
                      "extracted_fields": {
                        "symbol": "BTCUSDT",
                        "direction": "LONG",
                        "entry_price_min": 68000,
                        "entry_price_max": 68200,
                        "stop_loss": 67400,
                        "take_profit": [69000, 70000]
                      }
                    }
                    ```"""
                }
            }
        ]
    }
    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    result = client.classify_message(context)
    assert result.classification == "NEW_SIGNAL"
    assert result.action == "open"
    assert result.symbol == "BTCUSDT"
    assert str(result.entry_low) == "68000"


@pytest.mark.skipif(
    not (
        os.getenv("RUN_AI_GATEWAY_INTEGRATION_TESTS") == "1"
        and os.getenv("AI_GATEWAY_ENABLED", "false").lower() == "true"
        and os.getenv("AI_GATEWAY_BASE_URL")
    ),
    reason="AI gateway integration test is explicitly guarded",
)
def test_optional_gateway_integration_guarded(context: AIMessageContext) -> None:
    client = AjilGatewayClient(
        base_url=os.environ["AI_GATEWAY_BASE_URL"],
        timeout_seconds=10,
    )
    result = client.classify_message(context)
    assert result.confidence >= 0
