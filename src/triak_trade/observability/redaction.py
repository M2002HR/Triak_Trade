"""Observability redaction helpers."""

from __future__ import annotations

from typing import Any

from triak_trade.verification.redaction import redact as _redact
from triak_trade.verification.redaction import redact_text as _redact_text


def redact(value: Any) -> Any:
    return _redact(value)


def redact_text(text: str, *, max_chars: int | None = None) -> str:
    redacted = _redact_text(text)
    if max_chars is not None and len(redacted) > max_chars:
        return redacted[: max_chars - 16] + "... [truncated]"
    return redacted
