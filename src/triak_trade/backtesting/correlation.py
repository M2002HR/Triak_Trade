"""Deterministic follow-up correlation for the real backtest.

The AI classifier proposes a ``related_signal_id`` for follow-up messages
(close, cancel, stop/target updates, risk-free), but real-world data shows it
is unreliable: it may be empty, the literal ``"unknown"``, or even a Telegram
message id instead of a signal id. Losing those follow-ups silently corrupts
signal tracking (e.g. a "take profit / close" instruction never reaching the
open position).

This module provides a pure, side-effect-free resolver that trusts a *valid*
AI id first and otherwise falls back deterministically:

    1. AI id, only if it maps to a live signal.
    2. reply_to chain -> the signal that owns the replied-to message.
    3. symbol match against active signals (single, else most recently updated).
    4. (optional) most recent active signal for an unmistakable follow-up action.

The resolver never mutates context; the caller performs attach/merge.
"""

from __future__ import annotations

from dataclasses import dataclass

from triak_trade.agents.context import ChannelContext
from triak_trade.core.symbols import same_market_symbol
from triak_trade.domain.enums import SignalAction, SignalStatus
from triak_trade.domain.models import ParsedSignal, RawTelegramMessage, SignalState

# AI sometimes emits these instead of a real signal id; treat as "no usable id".
_INVALID_AI_IDS = {"", "unknown", "none", "null", "n/a", "na", "-"}

# Statuses that mean the signal is no longer trackable for new follow-ups.
_TERMINAL_STATUSES = {
    SignalStatus.CLOSED,
    SignalStatus.CANCELLED,
    SignalStatus.EXPIRED,
    SignalStatus.REJECTED,
    SignalStatus.INVALID,
}

_FOLLOW_UP_ACTIONS = {
    SignalAction.CLOSE,
    SignalAction.CANCEL,
    SignalAction.UPDATE_SL,
    SignalAction.UPDATE_TP,
    SignalAction.UPDATE_LEVERAGE,
    SignalAction.UPDATE_ENTRY,
}


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    """Outcome of follow-up correlation.

    ``signal_id`` is the resolved live signal id (a ``sig_...`` value that is a
    key in ``ChannelContext.active_signals``) or ``None`` when nothing matched.
    ``method`` and ``note`` exist purely for tracing/debug visibility.
    """

    signal_id: str | None
    method: str
    note: str | None = None


def is_invalid_ai_related_id(raw_related_id: str | None) -> bool:
    """True when the AI-provided related id cannot be a real signal id."""
    if raw_related_id is None:
        return True
    token = raw_related_id.strip().lower()
    if token in _INVALID_AI_IDS:
        return True
    # A bare integer is a Telegram message id, never a signal id (sig_...).
    if token.isdigit():
        return True
    return False


def _is_trackable(signal: SignalState) -> bool:
    return signal.status not in _TERMINAL_STATUSES


def resolve_related_signal_id(
    *,
    context: ChannelContext,
    parsed: ParsedSignal,
    raw_related_id: str | None,
    message: RawTelegramMessage,
    action: SignalAction,
    allow_last_resort: bool = False,
) -> CorrelationResult:
    """Resolve which active signal a follow-up message belongs to.

    See module docstring for the resolution order. Pure: does not mutate
    ``context``.
    """
    # 1) Trust a valid AI id.
    if not is_invalid_ai_related_id(raw_related_id):
        assert raw_related_id is not None
        if context.get_signal(raw_related_id) is not None:
            return CorrelationResult(signal_id=raw_related_id, method="ai")
    ai_note = (
        None
        if raw_related_id is None
        else f"ai_related_id_unresolved={raw_related_id}"
    )

    # 2) reply_to chain.
    by_reply = context.find_signal_by_message_reply(message.reply_to_msg_id)
    if by_reply is not None:
        return CorrelationResult(signal_id=by_reply.signal_id, method="reply_to", note=ai_note)
    for parent in context.get_reply_chain(message):
        owner = context.find_signal_by_message_reply(parent.message_id)
        if owner is not None:
            return CorrelationResult(
                signal_id=owner.signal_id, method="reply_chain", note=ai_note
            )

    # 3) Symbol match against active, trackable signals.
    if parsed.symbol is not None:
        same_symbol = [
            signal
            for signal in context.active_signals.values()
            if _is_trackable(signal)
            and signal.current_signal is not None
            and same_market_symbol(signal.current_signal.symbol, parsed.symbol)
        ]
        if len(same_symbol) == 1:
            return CorrelationResult(
                signal_id=same_symbol[0].signal_id, method="symbol_single", note=ai_note
            )
        if len(same_symbol) > 1:
            most_recent = max(same_symbol, key=lambda s: s.updated_at)
            return CorrelationResult(
                signal_id=most_recent.signal_id, method="symbol_recent", note=ai_note
            )

    # 4) No symbol on the follow-up, but exactly one signal is still open: an
    #    unmistakable directive ("سیو سود کنید" / "close" / "move SL") can only
    #    mean that one. High precision, so it is on by default (not gated behind
    #    the last-resort flag, which exists for the *ambiguous* multi-signal case).
    if action in _FOLLOW_UP_ACTIONS:
        trackable = [s for s in context.active_signals.values() if _is_trackable(s)]
        if len(trackable) == 1:
            return CorrelationResult(
                signal_id=trackable[0].signal_id, method="single_active", note=ai_note
            )

    # 5) Last resort: ambiguous follow-up with several open signals; only if enabled.
    if allow_last_resort and action in _FOLLOW_UP_ACTIONS:
        trackable = [s for s in context.active_signals.values() if _is_trackable(s)]
        if trackable:
            most_recent = max(trackable, key=lambda s: s.updated_at)
            return CorrelationResult(
                signal_id=most_recent.signal_id,
                method="most_recent_followup",
                note=ai_note,
            )

    return CorrelationResult(
        signal_id=None,
        method="unattached",
        note=ai_note or "no_signal_for_followup",
    )
