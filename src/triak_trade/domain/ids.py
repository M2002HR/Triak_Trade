"""Deterministic ID helpers."""

from __future__ import annotations

import hashlib
import re

_SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9_-]+")


def _normalize_segment(value: str) -> str:
    normalized = _SAFE_SEGMENT.sub("-", value.strip())
    return normalized.strip("-").lower() or "x"


def _short_hash(payload: str, length: int = 16) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def make_signal_id(channel_id: str, message_id: int) -> str:
    """Build deterministic signal id from message identity."""
    base = f"{_normalize_segment(channel_id)}:{message_id}"
    return f"sig_{_short_hash(base)}"


def make_action_id(signal_id: str, action_type: str, version: int) -> str:
    """Build deterministic action id from signal/action/version tuple."""
    base = f"{_normalize_segment(signal_id)}:{_normalize_segment(action_type)}:{version}"
    return f"act_{_short_hash(base)}"


def make_client_order_id(prefix: str, action_id: str) -> str:
    """Build deterministic log-safe client order id."""
    normalized_prefix = _normalize_segment(prefix)[:12]
    digest = _short_hash(action_id, length=20)
    return f"{normalized_prefix}_{digest}"
