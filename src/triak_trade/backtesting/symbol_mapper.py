"""Symbol normalization helpers for real backtesting."""

from __future__ import annotations

import re

_KNOWN_QUOTES = ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH")


def market_symbol_candidates(raw: str | None) -> list[str]:
    """Build conservative exchange symbol candidates from noisy parser output."""
    if raw is None:
        return []

    compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper().strip()
    if not compact:
        return []

    candidates: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in candidates:
            candidates.append(value)

    if compact.endswith("USDUSDT") and len(compact) > len("USDUSDT"):
        add(compact[: -len("USDUSDT")] + "USDT")

    if compact.endswith("USD") and not compact.endswith("USDT") and len(compact) > 3:
        add(compact[:-3] + "USDT")

    for quote in _KNOWN_QUOTES:
        if compact.endswith(quote) and len(compact) > len(quote):
            add(compact)
            break
    else:
        if compact.isalpha() and len(compact) <= 10:
            add(f"{compact}USDT")
        add(compact)

    return candidates


def normalize_market_symbol(raw: str | None) -> str | None:
    """Normalize signal symbols into exchange-friendly compact symbols."""
    candidates = market_symbol_candidates(raw)
    return candidates[0] if candidates else None
