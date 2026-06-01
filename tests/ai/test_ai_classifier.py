from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx

from triak_trade.agents.context import ChannelContext
from triak_trade.ai.classifier import AIMessageClassifier
from triak_trade.ai.gateway_client import AjilGatewayClient
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import SignalAction
from triak_trade.domain.models import RawTelegramMessage


def _raw(text: str) -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id="c1",
        channel_username="u1",
        message_id=1,
        text=text,
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
    )


def _context() -> ChannelContext:
    return ChannelContext(channel_id="c1", max_message_limit=10, max_update_window_hours=48)


def _result_payload(classification: str, action: str) -> dict[str, object]:
    return {
        "classification": classification,
        "action": action,
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
        "confidence": "0.85",
        "reasoning_summary": "summary",
        "risk_notes": [],
        "requires_admin_confirmation": True,
        "raw_provider_metadata": {"provider": "mock"},
    }


def _client(payload: dict[str, object]) -> AjilGatewayClient:
    return AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )


def test_ai_classifier_maps_new_signal_open() -> None:
    classifier = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(_result_payload("NEW_SIGNAL", "open")),
    )
    result = classifier.classify(_raw("x"), _context())
    assert result.parsed_signal.action is SignalAction.OPEN


def test_ai_classifier_maps_cancel() -> None:
    classifier = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(_result_payload("CANCEL", "cancel")),
    )
    result = classifier.classify(_raw("x"), _context())
    assert result.parsed_signal.action is SignalAction.CANCEL


def test_ai_classifier_maps_result_and_ad_to_ignore() -> None:
    result_report = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(_result_payload("RESULT_REPORT", "ignore")),
    ).classify(_raw("x"), _context())
    assert result_report.parsed_signal.action is SignalAction.IGNORE

    ad = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(_result_payload("ADVERTISEMENT", "ignore")),
    ).classify(_raw("x"), _context())
    assert ad.parsed_signal.action is SignalAction.IGNORE


def test_ai_classifier_maps_ambiguous_to_unknown() -> None:
    classifier = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(_result_payload("AMBIGUOUS", "unknown")),
    )
    result = classifier.classify(_raw("x"), _context())
    assert result.parsed_signal.action is SignalAction.UNKNOWN


def test_ai_classifier_decimal_fields_are_decimal() -> None:
    classifier = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(_result_payload("NEW_SIGNAL", "open")),
    )
    result = classifier.classify(_raw("x"), _context())
    assert isinstance(result.parsed_signal.entry_low, Decimal)
    assert isinstance(result.parsed_signal.confidence, Decimal)


def test_ai_classifier_fallback_to_regex_on_failure() -> None:
    failing = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"error": "x"})),
    )
    settings = Settings(AI_CLASSIFIER_USE_REGEX_FALLBACK=True)
    classifier = AIMessageClassifier(settings=settings, gateway_client=failing)
    result = classifier.classify(_raw("cancel BTC signal"), _context())
    assert result.parsed_signal.action in {SignalAction.CANCEL, SignalAction.UNKNOWN}
    assert any("fallback" in note for note in result.debug_notes)


def test_ai_classifier_no_fallback_returns_safe_unknown() -> None:
    failing = AjilGatewayClient(
        base_url="http://mocked.local",
        timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"error": "x"})),
    )
    settings = Settings(AI_CLASSIFIER_USE_REGEX_FALLBACK=False)
    classifier = AIMessageClassifier(settings=settings, gateway_client=failing)
    result = classifier.classify(_raw("anything"), _context())
    assert result.parsed_signal.action is SignalAction.UNKNOWN


def test_ai_classifier_constructible_for_channel_agent_contract() -> None:
    classifier = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(_result_payload("NEW_SIGNAL", "open")),
    )
    assert classifier is not None
