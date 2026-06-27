"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from triak_trade.db.base import Base, TimestampMixin, utc_now


class TelegramMessageORM(TimestampMixin, Base):
    __tablename__ = "telegram_messages"
    __table_args__ = (
        UniqueConstraint("channel_id", "message_id", "version", name="uq_telegram_msg_version"),
        Index("ix_telegram_messages_channel_message", "channel_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(255), index=True)
    channel_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_to_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class NormalizedMessageORM(Base):
    __tablename__ = "normalized_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_message_db_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_messages.id", ondelete="CASCADE"),
        index=True,
    )
    normalized_text: Mapped[str] = mapped_column(Text)
    detected_symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    detected_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    language_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SignalORM(TimestampMixin, Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    channel_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(64))
    created_from_message_id: Mapped[int] = mapped_column(Integer)
    related_message_ids: Mapped[list[int]] = mapped_column(JSON)
    current_signal: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandleORM(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "open_time", "source", name="uq_candle_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    interval: Mapped[str] = mapped_column(String(32), index=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    high: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    low: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    close: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    source: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ChannelMetricsORM(Base):
    __tablename__ = "channel_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(255), index=True)
    from_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    to_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditLogORM(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(128), index=True)
    level: Mapped[str] = mapped_column(String(32), index=True)
    module: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    channel_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LLMCallLogORM(Base):
    __tablename__ = "llm_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LiveSessionORM(Base):
    __tablename__ = "live_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    trading_mode: Mapped[str] = mapped_column(String(16), index=True)
    primary_channel_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_update_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class LiveTradeORM(Base):
    __tablename__ = "live_trades"
    __table_args__ = (
        Index("ix_live_trades_session_status", "session_id", "status"),
        Index("ix_live_trades_session_opened", "session_id", "opened_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    signal_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(255), index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True)
    is_open: Mapped[bool] = mapped_column(Boolean, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class LiveMessageTraceORM(Base):
    __tablename__ = "live_message_traces"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "channel_id",
            "message_id",
            name="uq_live_message_trace_key",
        ),
        Index("ix_live_message_traces_session_date", "session_id", "message_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(255), index=True)
    message_id: Mapped[int] = mapped_column(Integer, index=True)
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trade_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    final_status: Mapped[str] = mapped_column(String(64), index=True)
    message_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class LiveSignalSnapshotORM(Base):
    __tablename__ = "live_signal_snapshots"
    __table_args__ = (
        UniqueConstraint("session_id", "signal_id", name="uq_live_signal_snapshot_key"),
        Index("ix_live_signal_snapshots_session_status", "session_id", "status_group"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    signal_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    status_group: Mapped[str] = mapped_column(String(32), index=True)
    trade_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
