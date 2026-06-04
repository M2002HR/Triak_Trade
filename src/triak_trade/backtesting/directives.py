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
