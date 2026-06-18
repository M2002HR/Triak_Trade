"""Backtest-specific directive extraction from source messages."""

from __future__ import annotations

import re
from decimal import Decimal

from triak_trade.domain.enums import EntryType, MarketType, SignalAction, TradeSide
from triak_trade.domain.models import ParsedSignal, RawTelegramMessage

_CLOSE_PERCENT_RE = re.compile(r"(?P<pct>\d{1,3})\s*%")
_BREAKEVEN_MARKERS = (
    "breakeven",
    "break even",
    "sl to be",
    "stop to be",
    "move sl to entry",
    "move stop to entry",
    "risk free",
    "risk-free",
    "riskfree",
    "ریسک فری",
    "سر به سر",
)
# Text that, on a follow-up message, unmistakably means "close / take profit
# this position" even when the AI mislabels the action.
_CLOSE_MARKERS = (
    "سیو سود",
    "سیوسود",
    "ببندید",
    "ببند",
    "کلوز",
    "close position",
    "take profit now",
    "تیک پروفیت",
)
_CLOSE_ALL_MARKERS = (
    "ببندید همه",
    "ببند همه",
    "همه سیگنال",
    "همه پوزیشن",
    "all signals",
    "close all",
    "close everything",
    "close all positions",
)


def extract_close_fraction(text: str | None) -> Decimal | None:
    if not text:
        return None
    match = _CLOSE_PERCENT_RE.search(text.lower())
    if match is None:
        return None
    value = Decimal(match.group("pct"))
    if value <= Decimal("0"):
        return None
    if value >= Decimal("100"):
        return Decimal("1")
    return value / Decimal("100")


def detect_move_stop_to_entry(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _BREAKEVEN_MARKERS)


def detect_close_instruction(text: str | None) -> bool:
    """True when a follow-up clearly instructs closing/taking profit."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _CLOSE_MARKERS)


def detect_close_all_instruction(text: str | None) -> bool:
    """True when a follow-up clearly instructs closing all open signals."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _CLOSE_ALL_MARKERS)


_TP_LIST_MARKERS = (
    "tp list",
    "tplist",
    "tp:",
    "take profit list",
    "target list",
    "targets",
    "تارگت",
    "اهداف",
    "حد سود",
)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def detect_tp_list_update(text: str | None) -> list[Decimal]:
    """Extract a take-profit ladder from a bare "Tp List" follow-up.

    Channels often post just a row of prices with a "Tp List"/"تارگت" tag as an
    update to an active signal. The AI frequently marks these AMBIGUOUS (unknown
    action), which would drop the directive. When the text unmistakably carries a
    target list with two or more prices, return them so the caller can apply an
    UPDATE_TP instead of losing the message.
    """
    if not text:
        return []
    lowered = text.lower()
    if not any(marker in lowered for marker in _TP_LIST_MARKERS):
        return []
    values = [Decimal(match) for match in _NUMBER_RE.findall(text.replace(",", ""))]
    return values if len(values) >= 2 else []


def apply_text_directive_action(action: SignalAction, text: str | None) -> SignalAction:
    """Coerce a follow-up action from unmistakable text directives.

    The AI sometimes returns ``UNKNOWN``/``IGNORE``/``OPEN`` for messages that
    are plainly stop-to-entry or close instructions. When the action is not
    already a more specific follow-up action, promote it from the text so the
    directive reaches the simulator attached to the right signal.

    Move-stop-to-entry takes precedence over close so a single "risk free, save
    profit" message moves the stop rather than fully closing.
    """
    if action in {SignalAction.UPDATE_TP, SignalAction.UPDATE_LEVERAGE, SignalAction.CANCEL}:
        return action
    if detect_move_stop_to_entry(text):
        return SignalAction.UPDATE_SL
    if action is SignalAction.CLOSE:
        return action
    if detect_close_instruction(text):
        return SignalAction.CLOSE
    return action


def build_ignored_signal(
    message: RawTelegramMessage,
    *,
    invalid_reason: str,
) -> ParsedSignal:
    return ParsedSignal(
        action=SignalAction.IGNORE,
        market=MarketType.UNKNOWN,
        symbol=None,
        side=TradeSide.UNKNOWN,
        entry_type=EntryType.UNKNOWN,
        entry_low=None,
        entry_high=None,
        stop_loss=None,
        take_profits=[],
        leverage=None,
        confidence=Decimal("0"),
        invalid_reason=invalid_reason,
        source_channel_id=message.channel_id,
        source_message_id=message.message_id,
        parser_version="backtest-runtime-v1",
    )


def normalize_related_signal_action(parsed: ParsedSignal, *, is_related: bool) -> SignalAction:
    if not is_related or parsed.action is not SignalAction.OPEN:
        return parsed.action

    has_entry = (
        parsed.entry_low is not None
        or parsed.entry_high is not None
        or parsed.entry_type is EntryType.MARKET
        or parsed.entry_type is EntryType.LIMIT
        or parsed.entry_type is EntryType.RANGE
    )
    has_stop = parsed.stop_loss is not None
    has_tp = bool(parsed.take_profits)
    has_leverage = parsed.leverage is not None

    if has_stop and not has_tp and not has_leverage and not has_entry:
        return SignalAction.UPDATE_SL
    if has_tp and not has_stop and not has_leverage and not has_entry:
        return SignalAction.UPDATE_TP
    if has_leverage and not has_stop and not has_tp and not has_entry:
        return SignalAction.UPDATE_LEVERAGE
    return SignalAction.UPDATE_ENTRY
