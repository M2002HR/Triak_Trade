"""Prompt contract for AI message classification."""

from __future__ import annotations

from triak_trade.ai.schemas import AIMessageContext


def build_telegram_signal_prompt(context: AIMessageContext) -> str:
    """Build strict JSON-only prompt for AI classification."""
    return (
        "You are a Telegram trading-signal classifier. "
        "Classify message with channel context and return JSON only. "
        "Categories: new signal, signal update, cancellation, close instruction, "
        "TP/SL update, leverage update, result/profit report, advertisement, unrelated, ambiguous. "
        "Extract structured fields only. Do not invent missing SL/TP/entry values. "
        "If unsure use AMBIGUOUS. "
        "Do not treat profit reports like TP hit, SL hit, +120% profit as new signals. "
        "Do not execute trades. Do not provide private chain-of-thought. "
        "Provide only short reasoning_summary. "
        "Logic must generalize across channels. "
        "Tofan_Trade is only a future test target, never a hard-coded rule. "
        f"Context channel_id={context.channel_id}, message_id={context.message_id}."
    )
