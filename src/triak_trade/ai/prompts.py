"""Prompt contract for AI message classification."""

from __future__ import annotations

from triak_trade.ai.schemas import AIMessageContext


def build_telegram_signal_prompt(context: AIMessageContext) -> str:
    """Build strict JSON-only prompt for AI classification."""
    return (
        "You are a Telegram trading-signal classifier. "
        "Classify message with channel context and return JSON only. "
        "Use exactly these classification enum values: "
        "NEW_SIGNAL, SIGNAL_UPDATE, CANCEL, CLOSE, RESULT_REPORT, "
        "ADVERTISEMENT, GENERAL_ANALYSIS, UNRELATED, AMBIGUOUS, UNKNOWN. "
        "Use exactly these action values where applicable: "
        "open, cancel, close, update_sl, update_tp, update_leverage, ignore, unknown. "
        "Categories: new signal, signal update, cancellation, close instruction, "
        "TP/SL update, leverage update, result/profit report, advertisement, unrelated, ambiguous. "
        "Required JSON keys: classification, action, market, symbol, side, entry_type, "
        "entry_low, entry_high, stop_loss, take_profits, leverage, related_signal_id, "
        "relation_reason, confidence, reasoning_summary, risk_notes, "
        "requires_admin_confirmation, raw_provider_metadata. "
        "Extract structured fields only. Do not invent missing SL/TP/entry values. "
        "If unsure use AMBIGUOUS. "
        "Do not treat profit reports like TP hit, SL hit, +120% profit as new signals. "
        "Do not execute trades. Do not provide private chain-of-thought. "
        "Provide only short reasoning_summary. "
        "Logic must generalize across channels. "
        "Tofan_Trade is only a future test target, never a hard-coded rule. "
        f"Context channel_id={context.channel_id}, message_id={context.message_id}."
    )
