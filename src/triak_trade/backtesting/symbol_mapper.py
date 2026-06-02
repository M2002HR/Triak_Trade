"""Symbol normalization helpers for real backtesting."""

from __future__ import annotations

import re

_KNOWN_QUOTES = ("USDT", "USDC", "BTC", "ETH")


def normalize_market_symbol(raw: str | None) -> str | None:
    """Normalize signal symbols into exchange-friendly compact symbols."""
    if raw is None:
        return None

    compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper().strip()
    if not compact:
        return None
    for quote in _KNOWN_QUOTES:
        if compact.endswith(quote) and len(compact) > len(quote):
            return compact
    if compact.isalpha() and len(compact) <= 8:
        return f"{compact}USDT"
    return compact

