"""initial storage schema

Revision ID: 20260602_0001
Revises: None
Create Date: 2026-06-02 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260602_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(length=255), nullable=False),
        sa.Column("channel_username", sa.String(length=255), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reply_to_msg_id", sa.Integer(), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel_id", "message_id", "version", name="uq_telegram_msg_version"),
    )
    op.create_index("ix_telegram_messages_channel_id", "telegram_messages", ["channel_id"])
    op.create_index("ix_telegram_messages_message_id", "telegram_messages", ["message_id"])
    op.create_index("ix_telegram_messages_date", "telegram_messages", ["date"])
    op.create_index(
        "ix_telegram_messages_channel_message",
        "telegram_messages",
        ["channel_id", "message_id"],
    )

    op.create_table(
        "normalized_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "telegram_message_db_id",
            sa.Integer(),
            sa.ForeignKey("telegram_messages.id"),
            nullable=False,
        ),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("detected_symbols", sa.JSON(), nullable=False),
        sa.Column("detected_keywords", sa.JSON(), nullable=False),
        sa.Column("language_hint", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_normalized_messages_telegram_message_db_id",
        "normalized_messages",
        ["telegram_message_db_id"],
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("created_from_message_id", sa.Integer(), nullable=False),
        sa.Column("related_message_ids", sa.JSON(), nullable=False),
        sa.Column("current_signal", sa.JSON(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("signal_id"),
    )
    op.create_index("ix_signals_signal_id", "signals", ["signal_id"])
    op.create_index("ix_signals_channel_id", "signals", ["channel_id"])

    op.create_table(
        "candles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("interval", sa.String(length=32), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(24, 12), nullable=False),
        sa.Column("high", sa.Numeric(24, 12), nullable=False),
        sa.Column("low", sa.Numeric(24, 12), nullable=False),
        sa.Column("close", sa.Numeric(24, 12), nullable=False),
        sa.Column("volume", sa.Numeric(24, 12), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("symbol", "interval", "open_time", "source", name="uq_candle_key"),
    )
    op.create_index("ix_candles_symbol", "candles", ["symbol"])
    op.create_index("ix_candles_interval", "candles", ["interval"])
    op.create_index("ix_candles_open_time", "candles", ["open_time"])

    op.create_table(
        "channel_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(length=255), nullable=False),
        sa.Column("from_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("to_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_channel_metrics_channel_id", "channel_metrics", ["channel_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event", sa.String(length=128), nullable=False),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column("module", sa.String(length=255), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("channel_id", sa.String(length=255), nullable=True),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column("action_id", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_event", "audit_logs", ["event"])
    op.create_index("ix_audit_logs_level", "audit_logs", ["level"])
    op.create_index("ix_audit_logs_correlation_id", "audit_logs", ["correlation_id"])
    op.create_index("ix_audit_logs_channel_id", "audit_logs", ["channel_id"])
    op.create_index("ix_audit_logs_signal_id", "audit_logs", ["signal_id"])
    op.create_index("ix_audit_logs_action_id", "audit_logs", ["action_id"])

    op.create_table(
        "llm_call_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("prompt_hash", sa.String(length=128), nullable=True),
        sa.Column("response_hash", sa.String(length=128), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_llm_call_logs_provider", "llm_call_logs", ["provider"])


def downgrade() -> None:
    op.drop_index("ix_llm_call_logs_provider", table_name="llm_call_logs")
    op.drop_table("llm_call_logs")

    op.drop_index("ix_audit_logs_action_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_signal_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_channel_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_correlation_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_level", table_name="audit_logs")
    op.drop_index("ix_audit_logs_event", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_channel_metrics_channel_id", table_name="channel_metrics")
    op.drop_table("channel_metrics")

    op.drop_index("ix_candles_open_time", table_name="candles")
    op.drop_index("ix_candles_interval", table_name="candles")
    op.drop_index("ix_candles_symbol", table_name="candles")
    op.drop_table("candles")

    op.drop_index("ix_signals_channel_id", table_name="signals")
    op.drop_index("ix_signals_signal_id", table_name="signals")
    op.drop_table("signals")

    op.drop_index("ix_normalized_messages_telegram_message_db_id", table_name="normalized_messages")
    op.drop_table("normalized_messages")

    op.drop_index("ix_telegram_messages_channel_message", table_name="telegram_messages")
    op.drop_index("ix_telegram_messages_date", table_name="telegram_messages")
    op.drop_index("ix_telegram_messages_message_id", table_name="telegram_messages")
    op.drop_index("ix_telegram_messages_channel_id", table_name="telegram_messages")
    op.drop_table("telegram_messages")
