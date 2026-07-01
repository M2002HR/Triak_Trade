"""Message normalizer."""

from __future__ import annotations

import logging
import re

from triak_trade.core.logging import log_event, safe_preview
from triak_trade.domain.models import NormalizedMessage, RawTelegramMessage

_PERSIAN_DIGITS = str.maketrans(
    "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9",
    "0123456789",
)
_ARABIC_DIGITS = str.maketrans(
    "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
    "0123456789",
)

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MARKDOWN_DECORATION_RE = re.compile(r"[*_`~]+")
_TAG_SYMBOL_RE = re.compile(
    r"[#$]((?=[A-Za-z0-9]{2,20}\b)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]+)"
)
_PAIR_SYMBOL_RE = re.compile(
    r"\b((?=[A-Za-z0-9]{2,20}\b)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]+)"
    r"\s*[/\-\s]\s*(USDT|USD|USDC)\b",
    re.IGNORECASE,
)
_COMPACT_SYMBOL_RE = re.compile(
    r"\b((?=[A-Za-z0-9]{2,20}(?:USDT|USD|USDC|BTC|ETH)\b)"
    r"(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]+?)"
    r"(USDT|USD|USDC|BTC|ETH)\b",
    re.IGNORECASE,
)

_KEYWORDS = {
    "long",
    "short",
    "buy",
    "sell",
    "entry",
    "entries",
    "zone",
    "stop",
    "stoploss",
    "stop loss",
    "sl",
    "tp",
    "target",
    "targets",
    "leverage",
    "lev",
    "cancel",
    "cancelled",
    "close",
    "closed",
    "update",
    "move",
    "breakeven",
    "break even",
    "be",
    "\u062e\u0631\u06cc\u062f",
    "\u0641\u0631\u0648\u0634",
    "\u0644\u0627\u0646\u06af",
    "\u0634\u0648\u0631\u062a",
    "\u0648\u0631\u0648\u062f",
    "\u062d\u062f \u0636\u0631\u0631",
    "\u062a\u0627\u0631\u06af\u062a",
    "\u0627\u0647\u0631\u0645",
    "\u0644\u063a\u0648",
    "\u0628\u0633\u062a\u0646",
    "\u0645\u0627\u0631\u06a9\u062a",
}

_NON_SYMBOL_TAGS = {
    "LONG",
    "SHORT",
    "BUY",
    "SELL",
    "MARKET",
    "SPOT",
    "FUTURES",
    "ENTRY",
    "ENTRIES",
    "SL",
    "TP",
    "TARGET",
    "TARGETS",
    "LEV",
    "LEVERAGE",
}

_log = logging.getLogger(__name__)


class MessageNormalizer:
    """Deterministic message normalizer."""

    @staticmethod
    def _normalize_text(text: str) -> str:
        value = text.translate(_PERSIAN_DIGITS).translate(_ARABIC_DIGITS)
        value = _MARKDOWN_LINK_RE.sub(r"\1", value)
        value = _URL_RE.sub(" ", value)
        value = _MARKDOWN_DECORATION_RE.sub(" ", value)
        value = value.replace("\u066b", ".").replace("\u060c", ",")
        value = value.replace("\n", " ")
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @staticmethod
    def _compact_symbol(base: str, quote: str | None = None) -> str:
        base_norm = re.sub(r"[^A-Za-z0-9]", "", base.strip().upper())
        if quote is None:
            return base_norm
        return f"{base_norm}{re.sub(r'[^A-Za-z0-9]', '', quote.strip().upper())}"

    def normalize(self, raw: RawTelegramMessage) -> NormalizedMessage:
        text = raw.text or ""
        normalized = self._normalize_text(text)

        symbols: list[str] = []
        seen: set[str] = set()

        for match in _TAG_SYMBOL_RE.finditer(normalized):
            symbol = self._compact_symbol(match.group(1))
            if symbol in _NON_SYMBOL_TAGS:
                continue
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)

        for match in _PAIR_SYMBOL_RE.finditer(normalized):
            symbol = self._compact_symbol(match.group(1), match.group(2))
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)

        for match in _COMPACT_SYMBOL_RE.finditer(normalized):
            symbol = self._compact_symbol(match.group(1), match.group(2))
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)

        lowered = normalized.lower()
        keywords: list[str] = []
        seen_kw: set[str] = set()
        for keyword in _KEYWORDS:
            if keyword in lowered or keyword in normalized:
                kw = keyword.lower()
                if kw not in seen_kw:
                    seen_kw.add(kw)
                    keywords.append(kw)

        result = NormalizedMessage(
            raw=raw,
            normalized_text=normalized,
            detected_symbols=symbols,
            detected_keywords=keywords,
            language_hint=None,
        )
        log_event(
            _log,
            logging.DEBUG,
            "message_normalizer.normalized",
            channel_id=raw.channel_id,
            message_id=raw.message_id,
            raw_text_chars=len(text),
            normalized_text_chars=len(normalized),
            detected_symbols=symbols,
            detected_keyword_count=len(keywords),
            preview=safe_preview(normalized),
        )
        return result
