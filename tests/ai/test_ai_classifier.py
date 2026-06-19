from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx

from triak_trade.agents.classifier import RegexMessageClassifier
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
        "confidence": "0.85",
        "reasoning_summary": "summary",
        "risk_notes": [],
        "ignored_numeric_tokens": [],
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
    assert "classifier=ai" in result.debug_notes
    assert "ai_route_provider=groq" in result.debug_notes


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


def test_ai_classifier_skips_analysis_messages_before_ai_logic() -> None:
    classifier = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(_result_payload("GENERAL_ANALYSIS", "ignore")),
    )
    result = classifier.classify(_raw("Analysis\nXLM / 4H"), _context())
    assert result.parsed_signal.action is SignalAction.IGNORE
    assert "classification_skipped=skip_keyword:analysis" in result.debug_notes


def test_ai_classifier_skips_hashtag_analysis_messages_before_ai_logic() -> None:
    classifier = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(_result_payload("GENERAL_ANALYSIS", "ignore")),
    )
    result = classifier.classify(_raw("#Analysis\nBTC LONG update"), _context())
    assert result.parsed_signal.action is SignalAction.IGNORE
    assert "classification_skipped=skip_keyword:analysis" in result.debug_notes


def test_ai_classifier_skip_keywords_are_case_insensitive_and_win_over_include() -> None:
    classifier = AIMessageClassifier(
        settings=Settings(
            _env_file=None,
            AI_CLASSIFIER_FORCE_INCLUDE_KEYWORDS=["entry", "long"],
            AI_CLASSIFIER_SKIP_KEYWORDS=["AnALySis"],
        ),
        gateway_client=_client(_result_payload("NEW_SIGNAL", "open")),
    )
    result = classifier.classify(_raw("ENTRY setup\n#analysis"), _context())
    assert result.parsed_signal.action is SignalAction.IGNORE
    assert "classification_skipped=skip_keyword:analysis" in result.debug_notes


def test_ai_classifier_requires_force_include_keyword_outside_test_bypass() -> None:
    import sys

    classifier = AIMessageClassifier(
        settings=Settings(
            _env_file=None,
            APP_ENV="dev",
            AI_CLASSIFIER_FORCE_INCLUDE_KEYWORDS=["entry", "target"],
            AI_CLASSIFIER_SKIP_KEYWORDS=[],
        ),
        gateway_client=_client(_result_payload("NEW_SIGNAL", "open")),
    )
    pytest_module = sys.modules.pop("pytest", None)
    try:
        result = classifier.classify(_raw("general chat without trigger words"), _context())
    finally:
        if pytest_module is not None:
            sys.modules["pytest"] = pytest_module
    assert result.parsed_signal.action is SignalAction.IGNORE
    assert "classification_skipped=missing_force_include_keyword" in result.debug_notes


def test_ai_classifier_downgrades_inconsistent_new_signal_to_unknown() -> None:
    payload = _result_payload("NEW_SIGNAL", "ignore")
    payload["symbol"] = None
    payload["confidence"] = "0.10"
    classifier = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(payload),
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
    classifier = AIMessageClassifier(
        settings=settings,
        gateway_client=failing,
        regex_fallback=RegexMessageClassifier(),
    )
    result = classifier.classify(_raw("cancel BTC signal"), _context())
    assert result.parsed_signal.action in {SignalAction.CANCEL, SignalAction.UNKNOWN}
    assert any("fallback" in note for note in result.debug_notes)
    assert "classifier=regex" in result.debug_notes


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


def test_ai_classifier_uses_gemini_route_for_caption_with_image() -> None:
    raw = _raw("caption signal")
    raw = raw.model_copy(
        update={
            "raw_payload": {
                "has_media": True,
                "caption_present": True,
                "image_data_urls": [
                    {
                        "mime_type": "image/jpeg",
                        "data_url": "data:image/jpeg;base64,ZmFrZQ==",
                    }
                ],
            }
        }
    )
    classifier = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(_result_payload("NEW_SIGNAL", "open")),
    )
    result = classifier.classify(raw, _context())
    assert "ai_route_provider=gemini" in result.debug_notes
    assert "ai_route_multimodal=True" in result.debug_notes
    assert "ai_route_model=gemini-3.1-flash-lite" in result.debug_notes


def test_ai_classifier_accepts_context_extracted_fields_from_ai() -> None:
    payload = _result_payload("NEW_SIGNAL", "open")
    payload["stop_loss"] = "67400"
    payload["take_profits"] = ["69000", "70000", "71500"]
    payload["source_message_ids"] = [1, 2, 3]
    payload["extracted_from_context"] = True
    classifier = AIMessageClassifier(
        settings=Settings(),
        gateway_client=_client(payload),
    )
    first = _raw("BTCUSDT LONG Entry: 68000 - 68200")
    second = _raw("Targets: 69000 / 70000 / 71500").model_copy(update={"message_id": 2})
    third = _raw("Stoploss: 67400").model_copy(update={"message_id": 3})
    context = _context()
    context.seed_message_catalog([first, second, third])
    context.add_recent_message(first)
    result = classifier.classify(first, context)
    assert [str(item) for item in result.parsed_signal.take_profits] == ["69000", "70000", "71500"]
    assert str(result.parsed_signal.stop_loss) == "67400"


def test_ai_classifier_sanitizes_noisy_take_profit_numbers() -> None:
    payload = {
        "classification": "NEW_SIGNAL",
        "action": "open",
        "market": "futures",
        "symbol": "NMR-SWAP-USDT",
        "side": "short",
        "entry_type": "market",
        "entry_low": None,
        "entry_high": None,
        "stop_loss": "8.85",
        "take_profits": ["1", "8.377", "2", "8.310", "3", "8.226", "4", "7.990", "220", "166"],
        "leverage": 20,
        "related_signal_id": None,
        "relation_reason": None,
        "confidence": "1.0",
        "reasoning_summary": "clear signal",
        "risk_notes": [],
        "requires_admin_confirmation": True,
        "raw_provider_metadata": {"provider": "mock"},
    }
    classifier = AIMessageClassifier(
        settings=Settings(_env_file=None),
        gateway_client=_client(payload),
    )
    message = _raw(
        """
        NMR/USDT SHORT
        Market
        TP1 8.377
        TP2 8.310
        TP3 8.226
        TP4 7.990
        STOPLOSS 8.85
        [Trade on Toobit](https://t.me/Tofan_Trade/220)
        [Capital management](https://t.me/Tofan_Trade/166)
        """
    )
    result = classifier.classify(message, _context())
    assert [str(item) for item in result.parsed_signal.take_profits] == [
        "8.377",
        "8.310",
        "8.226",
        "7.990",
    ]


def test_ai_classifier_backfills_missing_stop_loss_from_formatted_message() -> None:
    payload = _result_payload("NEW_SIGNAL", "open")
    payload["symbol"] = "VELVETUSD"
    payload["symbol_raw"] = "VELVET/USD"
    payload["entry_type"] = "market"
    payload["entry_low"] = None
    payload["entry_high"] = None
    payload["stop_loss"] = None
    payload["take_profits"] = ["0.42", "0.4428", "0.67"]
    payload["leverage"] = 20
    classifier = AIMessageClassifier(
        settings=Settings(_env_file=None),
        gateway_client=_client(payload),
    )
    message = _raw(
        """
        **سیگنال فیوچرز اتحاد**🥇

        VELVET/**USD ****🌪****

        ****🌪****LONG **🥇**🌪****

        **🥇**LEVERAGE: Cross
        20X

        Entry
        MARKET

        Targets
        1 0.42
        2 0.4428
        3 0.67

        STOPLOSS
        ⚠️
        🥇،0.386
        """
    )
    result = classifier.classify(message, _context())
    assert result.parsed_signal.action is SignalAction.OPEN
    assert result.parsed_signal.entry_type.value == "market"
    assert result.parsed_signal.stop_loss == Decimal("0.386")
    assert "regex_supplement=stop_loss" in result.debug_notes
