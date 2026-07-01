"""Dashboard state schemas."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


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


class SavedChannelEntry(BaseModel):
    channel_input: str
    channel_resolved: str
    label: str
    created_at: datetime


class SavedChannelsState(BaseModel):
    channels: list[SavedChannelEntry] = Field(default_factory=list)


class StrategyCatalogEntry(BaseModel):
    key: str
    name: str
    class_name: str
    active: bool = False
    description: str
    parameters: dict[str, object] = Field(default_factory=dict)


class AIKeywordFilterConfig(BaseModel):
    force_include_keywords: list[str] = Field(default_factory=list)
    skip_keywords: list[str] = Field(default_factory=list)
    config_path: str


class BacktestLifecycleConfig(BaseModel):
    refresh_interval: str
    config_path: str


class TelegramNotificationConfig(BaseModel):
    """Controls which live-trading events are forwarded to the Telegram log channel.

    Backtest events are intentionally excluded — they are too noisy for a chat channel.
    """

    enabled: bool = True
    # ── Live signal events ─────────────────────────────────────────────────
    send_signal_detected: bool = True
    send_signal_invalid: bool = False
    send_signal_ignored: bool = False
    # ── Live trade events ──────────────────────────────────────────────────
    send_trade_opened: bool = True
    send_trade_closed: bool = True
    send_trade_updated: bool = False
    # ── Session lifecycle events ───────────────────────────────────────────
    send_session_started: bool = True
    send_session_stopped: bool = True
    send_session_error: bool = True
    # ── Summary / digest ──────────────────────────────────────────────────
    send_session_summary: bool = True
    send_daily_digest: bool = False
    # ── Error alerts ──────────────────────────────────────────────────────
    send_error_alerts: bool = True
    # ── Metadata ──────────────────────────────────────────────────────────
    updated_at: datetime = Field(default_factory=utc_now)
    updated_by: str = "system"
