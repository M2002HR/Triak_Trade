"""Redaction helpers for verification reports."""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEYS = {
    "telegram_bot_token",
    "toobit_api_key",
    "toobit_api_secret",
    "telegram_api_hash",
    "gemini_api_keys",
    "groq_api_keys",
    "signature",
    "session",
    "authorization",
    "x-bb-apikey",
    "api_key",
    "api_secret",
    "token",
    "secret",
}

SENSITIVE_PATTERNS = [
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"signature=([a-fA-F0-9]{32,})"),
    re.compile(r"X-BB-APIKEY[:=]\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"Authorization[:=]\s*[^,\s]+", re.IGNORECASE),
]


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                redacted[str(key)] = "***REDACTED***"
            else:
                redacted[str(key)] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    result = text
    for pattern in SENSITIVE_PATTERNS:
        result = pattern.sub("***REDACTED***", result)
    return result


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized.endswith("_present") or normalized.endswith("_enabled"):
        return False
    return any(part in normalized for part in SENSITIVE_KEYS)
