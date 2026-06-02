"""Dashboard state schemas."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AutoModeState(BaseModel):
    enabled: bool = False
    scope: str = "demo_only"
    updated_at: datetime
    updated_by: str = "system"
    reason: str = (
        "Auto Mode is stored but execution requires future Risk Engine + Demo Execution modules."
    )


class KillSwitchState(BaseModel):
    enabled: bool = False
    reason: str = ""
    updated_at: datetime
    updated_by: str = "system"


class DashboardRuntimeStatus(BaseModel):
    running: bool = False
    pid: int | None = None
    started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    url: str | None = None
    log_file: str | None = None
    last_error_type: str | None = None
    last_error_message_redacted: str | None = None
