"""Verification result models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class VerificationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    WARN = "WARN"


class VerificationCheckResult(BaseModel):
    name: str
    status: VerificationStatus
    category: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int
    error_type: str | None = None
    next_action: str | None = None
    safe_to_share: bool = True


class VerificationReport(BaseModel):
    generated_at: datetime
    environment_summary: dict[str, Any]
    checks: list[VerificationCheckResult]
    totals: dict[str, int]
    overall_status: VerificationStatus
    markdown_path: str | None = None
    json_path: str | None = None
