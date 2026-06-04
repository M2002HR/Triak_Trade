"""AI gateway request/response schemas."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class AIMessageContext(BaseModel):
    channel_id: str
    channel_username: str | None
    message_id: int
    message_text: str | None
    message_date: datetime
    message_has_media: bool = False
    message_is_caption: bool = False
    message_images: list[dict[str, Any]] = Field(default_factory=list)
    reply_chain_messages: list[dict[str, Any]] = Field(default_factory=list)
    following_messages: list[dict[str, Any]] = Field(default_factory=list)
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)
    active_signals: list[dict[str, Any]] = Field(default_factory=list)
    parser_version: str
    notes: list[str] = Field(default_factory=list)


class AIClassificationResult(BaseModel):
    classification: Literal[
        "NEW_SIGNAL",
        "SIGNAL_UPDATE",
        "CANCEL",
        "CLOSE",
        "RESULT_REPORT",
        "ADVERTISEMENT",
        "GENERAL_ANALYSIS",
        "UNRELATED",
        "AMBIGUOUS",
        "UNKNOWN",
    ]
    action: str
    market: str
    symbol: str | None
    symbol_raw: str | None = None
    side: str
    entry_type: str
    entry_low: Decimal | None
    entry_high: Decimal | None
    entry_prices: list[Decimal] = Field(default_factory=list)
    stop_loss: Decimal | None
    take_profits: list[Decimal] = Field(default_factory=list)
    leverage: int | None
    leverage_mode: str | None = None
    close_fraction: Decimal | None = None
    move_stop_to_entry: bool = False
    related_signal_id: str | None
    relation_reason: str | None
    source_message_ids: list[int] = Field(default_factory=list)
    extracted_from_context: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    confidence: Decimal
    reasoning_summary: str
    risk_notes: list[str] = Field(default_factory=list)
    ignored_numeric_tokens: list[str] = Field(default_factory=list)
    requires_admin_confirmation: bool
    raw_provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: Decimal) -> Decimal:
        if value < Decimal("0") or value > Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
        return value

    @field_validator("close_fraction")
    @classmethod
    def validate_close_fraction(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if value <= Decimal("0") or value > Decimal("1"):
            raise ValueError("close_fraction must be between 0 and 1")
        return value

    @field_validator("entry_prices", "take_profits", mode="before")
    @classmethod
    def normalize_decimal_lists(cls, value: Any) -> list[Any]:
        def extract(raw: Any) -> list[Any]:
            if raw is None:
                return []
            if isinstance(raw, list):
                merged: list[Any] = []
                for item in raw:
                    merged.extend(extract(item))
                return merged
            if isinstance(raw, str):
                matches = re.findall(r"-?\d+(?:\.\d+)?", raw.replace(",", " "))
                return matches or [raw]
            return [raw]

        return extract(value)
