from __future__ import annotations

from triak_trade.ai.prompts import build_telegram_signal_prompt
from triak_trade.ai.schemas import AIMessageContext


def test_prompt_contract_contains_required_rules() -> None:
    prompt = build_telegram_signal_prompt(
        AIMessageContext(
            channel_id="c1",
            channel_username="u",
            message_id=1,
            message_text="BTC",
            message_date="2026-01-01T00:00:00Z",
            recent_messages=[],
            active_signals=[],
            parser_version="ai-v1",
            notes=[],
        )
    )
    assert "JSON only" in prompt
    assert "Do not invent missing SL/TP/entry values" in prompt
    assert "profit reports" in prompt
    assert "AMBIGUOUS" in prompt
    assert "Tofan_Trade" in prompt
