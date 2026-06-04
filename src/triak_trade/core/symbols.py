"""Shared symbol normalization helpers."""

from __future__ import annotations

import re

_KNOWN_QUOTES = ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH")


def canonical_market_symbol(raw: str | None) -> str | None:
    if raw is None:
        return None

    compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper().strip()
    if not compact:
        return None

    for quote in ("USDT", "USDC"):
        marker = f"SWAP{quote}"
        if compact.endswith(marker) and len(compact) > len(marker):
            compact = compact[: -len(marker)] + quote
            break

    if compact.endswith("USDUSDT") and len(compact) > len("USDUSDT"):
        compact = compact[: -len("USDUSDT")] + "USDT"

    if compact.endswith("USD") and not compact.endswith("USDT") and len(compact) > 3:
        compact = compact[:-3] + "USDT"

    for quote in _KNOWN_QUOTES:
        if compact.endswith(quote) and len(compact) > len(quote):
            break
    else:
        if compact.isalpha() and len(compact) <= 10:
            compact = f"{compact}USDT"

    return compact


def same_market_symbol(left: str | None, right: str | None) -> bool:
    left_normalized = canonical_market_symbol(left)
    right_normalized = canonical_market_symbol(right)
    return bool(left_normalized and right_normalized and left_normalized == right_normalized)


def futures_contract_symbol_candidates(raw: str | None) -> list[str]:
    normalized = canonical_market_symbol(raw)
    if normalized is None:
        return []

    candidates: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in candidates:
            candidates.append(value)

    if normalized.endswith("USDT") and len(normalized) > 4:
        base = normalized[:-4]
        add(f"{base}-SWAP-USDT")
    if normalized.endswith("USDC") and len(normalized) > 4:
        base = normalized[:-4]
        add(f"{base}-SWAP-USDC")

    return candidates


def futures_index_symbol_candidates(raw: str | None) -> list[str]:
    normalized = canonical_market_symbol(raw)
    return [normalized] if normalized else []


def market_symbol_candidates(raw: str | None) -> list[str]:
    candidates: list[str] = []

    def add_many(values: list[str]) -> None:
        for value in values:
            if value not in candidates:
                candidates.append(value)

    add_many(futures_contract_symbol_candidates(raw))
    add_many(futures_index_symbol_candidates(raw))
    return candidates


def normalize_market_symbol(raw: str | None) -> str | None:
    candidates = market_symbol_candidates(raw)
    return candidates[0] if candidates else None
