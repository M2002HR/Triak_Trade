"""Structured processing audit event models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from triak_trade.observability.redaction import redact_text


class ProcessingAuditStatus(str, Enum):
    SUCCESS = "SUCCESS"
    IGNORED = "IGNORED"
    AMBIGUOUS = "AMBIGUOUS"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class ProcessingAuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"audit_{uuid.uuid4().hex}")
    event_type: Literal["message_processed", "message_processing_error"] = "message_processed"
    channel_id: str
    channel_username: str | None = None
    message_id: int
    message_link: str | None = None
    message_date: datetime
    processing_started_at: datetime
    processing_finished_at: datetime
    duration_ms: int
    classifier_name: str
    classification: str
    parsed_action: str
    symbol: str | None = None
    side: str | None = None
    signal_id: str | None = None
    related_signal_id: str | None = None
    proposed_action_id: str | None = None
    proposed_action_type: str | None = None
    state_before: str | None = None
    state_after: str | None = None
    validation_passed: bool | None = None
    risk_increasing: bool | None = None
    status: ProcessingAuditStatus
    reason: str | None = None
    debug_notes: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_message_redacted: str | None = None
    safe_message_preview: str | None = None

    @field_validator(
        "channel_id",
        "channel_username",
        "message_link",
        "classifier_name",
        "classification",
        "parsed_action",
        "symbol",
        "side",
        "signal_id",
        "related_signal_id",
        "proposed_action_id",
        "proposed_action_type",
        "state_before",
        "state_after",
        "reason",
        "error_type",
        "error_message_redacted",
        "safe_message_preview",
        mode="before",
    )
    @classmethod
    def redact_string_fields(cls, value: Any) -> Any:
        if isinstance(value, str):
            return redact_text(value)
        return value

    @field_validator("debug_notes", mode="before")
    @classmethod
    def redact_debug_notes(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, list):
            return [redact_text(str(item)) for item in value]
        return [redact_text(str(value))]

    @model_validator(mode="after")
    def validate_timing(self) -> ProcessingAuditEvent:
        if self.processing_finished_at < self.processing_started_at:
            msg = "processing_finished_at cannot be before processing_started_at"
            raise ValueError(msg)
        if self.duration_ms < 0:
            msg = "duration_ms cannot be negative"
            raise ValueError(msg)
        return self


def build_message_link(channel_username: str | None, message_id: int) -> str | None:
    if channel_username is None:
        return None
    username = channel_username.strip()
    if not username:
        return None
    username = username.lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}/{message_id}"
