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
        message_has_media=False,
        message_is_caption=False,
        message_images=[],
        reply_chain_messages=[],
        following_messages=[],
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
        "symbol_raw": "BTC/USDT",
        "side": "long",
        "entry_type": "range",
        "entry_low": "68000",
        "entry_high": "68200",
        "entry_prices": ["68000", "68200"],
        "stop_loss": "67400",
        "take_profits": ["69000", "70000"],
        "leverage": 5,
        "leverage_mode": "cross",
        "close_fraction": None,
        "move_stop_to_entry": False,
        "related_signal_id": None,
        "relation_reason": None,
        "source_message_ids": [1],
        "extracted_from_context": False,
        "missing_fields": [],
        "confidence": "0.90",
        "reasoning_summary": "clear signal",
        "risk_notes": [],
        "ignored_numeric_tokens": [],
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
    assert "required_output_contract" in str(observed["json"])
    assert "required_output_schema" not in str(observed["json"])


def test_gateway_client_routes_text_requests_to_groq_model(context: AIMessageContext) -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["json"] = request.read().decode()
        return httpx.Response(200, json=_ok_payload())

    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    client.classify_message(context)
    payload = str(observed["json"])
    assert "openai/gpt-oss-120b" in payload
    assert "\"model\":\"openai/gpt-oss-120b\"" in payload


def test_gateway_client_routes_caption_images_to_gemini_multimodal() -> None:
    observed: dict[str, object] = {}
    context = AIMessageContext(
        channel_id="c1",
        channel_username="u1",
        message_id=2,
        message_text="caption text",
        message_date="2026-01-01T00:00:00Z",
        message_has_media=True,
        message_is_caption=True,
        message_images=[
            {
                "mime_type": "image/jpeg",
                "data_url": "data:image/jpeg;base64,ZmFrZQ==",
            }
        ],
        reply_chain_messages=[],
        following_messages=[],
        recent_messages=[],
        active_signals=[],
        parser_version="ai-v1",
        notes=[],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        observed["json"] = request.read().decode()
        return httpx.Response(200, json=_ok_payload())

    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    client.classify_message(context)
    payload = str(observed["json"])
    assert "gemini-3.1-flash-lite" in payload
    assert "image_url" in payload


def test_gateway_client_routes_arabic_text_requests_to_gemini_model() -> None:
    observed: dict[str, object] = {}
    message_text = "**$BTC**\n#SHORT\n#مارکت\nاهرم :70×"  # noqa: RUF001
    context = AIMessageContext(
        channel_id="c1",
        channel_username="u1",
        message_id=3,
        message_text=message_text,
        message_date="2026-01-01T00:00:00Z",
        reply_chain_messages=[],
        following_messages=[],
        recent_messages=[],
        active_signals=[],
        parser_version="ai-v1",
        notes=[],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        observed["json"] = request.read().decode()
        return httpx.Response(200, json=_ok_payload())

    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    client.classify_message(context)
    payload = str(observed["json"])
    assert "gemini-3.1-flash-lite" in payload
    assert "\"model\":\"gemini-3.1-flash-lite\"" in payload


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
            lambda _: httpx.Response(200, json={"classification": {"not": "valid"}})
        ),
    )
    with pytest.raises(AIGatewayResponseError):
        client.classify_message(context)


def test_gateway_client_retries_after_invalid_response_and_succeeds(
    context: AIMessageContext,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"bad": "payload"})
        return httpx.Response(200, json=_ok_payload())

    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        retry_attempts=2,
        retry_backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    result = client.classify_message(context)
    assert calls == 2
    assert result.symbol == "BTCUSDT"


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


def test_gateway_client_normalizes_textual_confidence_and_nullable_fields(
    context: AIMessageContext,
) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": """```json
                    {
                      "classification": "ADVERTISEMENT",
                      "action": "ignore",
                      "market": null,
                      "symbol": null,
                      "side": null,
                      "entry_type": null,
                      "entry_low": null,
                      "entry_high": null,
                      "stop_loss": null,
                      "take_profits": [],
                      "leverage": null,
                      "related_signal_id": null,
                      "relation_reason": null,
                      "confidence": "high",
                      "reasoning_summary": "promo message",
                      "risk_notes": [],
                      "requires_admin_confirmation": false,
                      "raw_provider_metadata": {}
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
    assert result.classification == "ADVERTISEMENT"
    assert str(result.confidence) == "0.85"


def test_gateway_client_tolerates_string_provider_metadata(
    context: AIMessageContext,
) -> None:
    # Regression: some providers return raw_provider_metadata as a string. The old
    # code called dict("text") which raised ValueError and (with no regex fallback)
    # aborted the whole backtest. The gateway must normalize it without raising.
    payload = {
        "choices": [
            {
                "message": {
                    "content": """```json
                    {
                      "classification": "new signal",
                      "reasoning_summary": "clear signal",
                      "raw_provider_metadata": "gemini-3.1-flash-lite",
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
    assert result.symbol == "BTCUSDT"


def test_gateway_client_coerces_thousands_separated_prices_and_leverage_range(
    context: AIMessageContext,
) -> None:
    # Channels post prices with thousands separators and leverage as a range.
    # These must coerce instead of failing schema validation (dropping the signal).
    payload = _ok_payload()
    payload["entry_low"] = "61,000"
    payload["entry_high"] = "66,000"
    payload["stop_loss"] = "58,500"
    payload["leverage"] = "40-60"
    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    result = client.classify_message(context)
    assert str(result.entry_low) == "61000"
    assert str(result.entry_high) == "66000"
    assert str(result.stop_loss) == "58500"
    assert result.leverage == 40


def test_gateway_client_splits_take_profit_string(context: AIMessageContext) -> None:
    payload = _ok_payload()
    payload["take_profits"] = "69000 / 70000 / 71500"
    client = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    result = client.classify_message(context)
    assert [str(item) for item in result.take_profits] == ["69000", "70000", "71500"]


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
        auth_token=os.getenv("AI_GATEWAY_AUTH_TOKEN", ""),
    )
    result = client.classify_message(context)
    assert result.confidence >= 0
