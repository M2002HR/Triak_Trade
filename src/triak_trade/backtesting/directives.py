"""Backtest-specific directive extraction from source messages."""

from __future__ import annotations

import re
from decimal import Decimal

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
