"""AI gateway request/response schemas."""

from __future__ import annotations

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
    side: str
    entry_type: str
    entry_low: Decimal | None
    entry_high: Decimal | None
    stop_loss: Decimal | None
    take_profits: list[Decimal] = Field(default_factory=list)
    leverage: int | None
    related_signal_id: str | None
    relation_reason: str | None
    confidence: Decimal
    reasoning_summary: str
    risk_notes: list[str] = Field(default_factory=list)
    requires_admin_confirmation: bool
    raw_provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: Decimal) -> Decimal:
        if value < Decimal("0") or value > Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
        return value
