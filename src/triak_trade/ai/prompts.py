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
        "Use reply-chain context and up to the next 3 following messages when they look like "
        "continuations of the same signal. "
        "Messages may split symbol/side, targets, and stop-loss across separate posts. "
        "If a reply updates, cancels, closes, or changes stop-loss/take-profit/leverage "
        "for a previous signal, classify it as a related update, cancel, or close "
        "instead of a new signal. "
        "If the message is a caption or includes image context, use the image "
        "together with the text. "
        "Do not execute trades. Do not provide private chain-of-thought. "
        "Provide only short reasoning_summary. "
        "Logic must generalize across channels. "
        "Tofan_Trade is only a future test target, never a hard-coded rule. "
        f"Context channel_id={context.channel_id}, message_id={context.message_id}, "
        f"message_has_media={context.message_has_media}, "
        f"message_is_caption={context.message_is_caption}."
    )
