"""live trading state storage

Revision ID: 20260627_0002
Revises: 20260602_0001
Create Date: 2026-06-27 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260627_0002"
down_revision = "20260602_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("trading_mode", sa.String(length=16), nullable=False),
        sa.Column("primary_channel_id", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_update_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("session_id"),
    )
    op.create_index("ix_live_sessions_session_id", "live_sessions", ["session_id"])
    op.create_index("ix_live_sessions_status", "live_sessions", ["status"])
    op.create_index("ix_live_sessions_trading_mode", "live_sessions", ["trading_mode"])
    op.create_index("ix_live_sessions_primary_channel_id", "live_sessions", ["primary_channel_id"])
    op.create_index("ix_live_sessions_started_at", "live_sessions", ["started_at"])
    op.create_index("ix_live_sessions_last_update_at", "live_sessions", ["last_update_at"])

    op.create_table(
        "live_trades",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=255), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("trade_id"),
    )
    op.create_index("ix_live_trades_trade_id", "live_trades", ["trade_id"])
    op.create_index("ix_live_trades_session_id", "live_trades", ["session_id"])
    op.create_index("ix_live_trades_signal_id", "live_trades", ["signal_id"])
    op.create_index("ix_live_trades_channel_id", "live_trades", ["channel_id"])
    op.create_index("ix_live_trades_symbol", "live_trades", ["symbol"])
    op.create_index("ix_live_trades_status", "live_trades", ["status"])
    op.create_index("ix_live_trades_is_open", "live_trades", ["is_open"])
    op.create_index("ix_live_trades_opened_at", "live_trades", ["opened_at"])
    op.create_index("ix_live_trades_updated_at", "live_trades", ["updated_at"])
    op.create_index(
        "ix_live_trades_session_status",
        "live_trades",
        ["session_id", "status"],
    )
    op.create_index(
        "ix_live_trades_session_opened",
        "live_trades",
        ["session_id", "opened_at"],
    )

    op.create_table(
        "live_message_traces",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=255), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column("trade_id", sa.String(length=64), nullable=True),
        sa.Column("final_status", sa.String(length=64), nullable=False),
        sa.Column("message_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "session_id",
            "channel_id",
            "message_id",
            name="uq_live_message_trace_key",
        ),
    )
    op.create_index("ix_live_message_traces_session_id", "live_message_traces", ["session_id"])
    op.create_index("ix_live_message_traces_channel_id", "live_message_traces", ["channel_id"])
    op.create_index("ix_live_message_traces_message_id", "live_message_traces", ["message_id"])
    op.create_index("ix_live_message_traces_signal_id", "live_message_traces", ["signal_id"])
    op.create_index("ix_live_message_traces_trade_id", "live_message_traces", ["trade_id"])
    op.create_index(
        "ix_live_message_traces_final_status",
        "live_message_traces",
        ["final_status"],
    )
    op.create_index(
        "ix_live_message_traces_message_date",
        "live_message_traces",
        ["message_date"],
    )
    op.create_index(
        "ix_live_message_traces_received_at",
        "live_message_traces",
        ["received_at"],
    )
    op.create_index(
        "ix_live_message_traces_session_date",
        "live_message_traces",
        ["session_id", "message_date"],
    )

    op.create_table(
        "live_signal_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_group", sa.String(length=32), nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("session_id", "signal_id", name="uq_live_signal_snapshot_key"),
    )
    op.create_index("ix_live_signal_snapshots_session_id", "live_signal_snapshots", ["session_id"])
    op.create_index("ix_live_signal_snapshots_signal_id", "live_signal_snapshots", ["signal_id"])
    op.create_index("ix_live_signal_snapshots_channel_id", "live_signal_snapshots", ["channel_id"])
    op.create_index("ix_live_signal_snapshots_status", "live_signal_snapshots", ["status"])
    op.create_index(
        "ix_live_signal_snapshots_status_group",
        "live_signal_snapshots",
        ["status_group"],
    )
    op.create_index("ix_live_signal_snapshots_trade_id", "live_signal_snapshots", ["trade_id"])
    op.create_index("ix_live_signal_snapshots_symbol", "live_signal_snapshots", ["symbol"])
    op.create_index("ix_live_signal_snapshots_updated_at", "live_signal_snapshots", ["updated_at"])
    op.create_index(
        "ix_live_signal_snapshots_session_status",
        "live_signal_snapshots",
        ["session_id", "status_group"],
    )


def downgrade() -> None:
    op.drop_index("ix_live_signal_snapshots_session_status", table_name="live_signal_snapshots")
    op.drop_index("ix_live_signal_snapshots_updated_at", table_name="live_signal_snapshots")
    op.drop_index("ix_live_signal_snapshots_symbol", table_name="live_signal_snapshots")
    op.drop_index("ix_live_signal_snapshots_trade_id", table_name="live_signal_snapshots")
    op.drop_index("ix_live_signal_snapshots_status_group", table_name="live_signal_snapshots")
    op.drop_index("ix_live_signal_snapshots_status", table_name="live_signal_snapshots")
    op.drop_index("ix_live_signal_snapshots_channel_id", table_name="live_signal_snapshots")
    op.drop_index("ix_live_signal_snapshots_signal_id", table_name="live_signal_snapshots")
    op.drop_index("ix_live_signal_snapshots_session_id", table_name="live_signal_snapshots")
    op.drop_table("live_signal_snapshots")

    op.drop_index("ix_live_message_traces_session_date", table_name="live_message_traces")
    op.drop_index("ix_live_message_traces_received_at", table_name="live_message_traces")
    op.drop_index("ix_live_message_traces_message_date", table_name="live_message_traces")
    op.drop_index("ix_live_message_traces_final_status", table_name="live_message_traces")
    op.drop_index("ix_live_message_traces_trade_id", table_name="live_message_traces")
    op.drop_index("ix_live_message_traces_signal_id", table_name="live_message_traces")
    op.drop_index("ix_live_message_traces_message_id", table_name="live_message_traces")
    op.drop_index("ix_live_message_traces_channel_id", table_name="live_message_traces")
    op.drop_index("ix_live_message_traces_session_id", table_name="live_message_traces")
    op.drop_table("live_message_traces")

    op.drop_index("ix_live_trades_session_opened", table_name="live_trades")
    op.drop_index("ix_live_trades_session_status", table_name="live_trades")
    op.drop_index("ix_live_trades_updated_at", table_name="live_trades")
    op.drop_index("ix_live_trades_opened_at", table_name="live_trades")
    op.drop_index("ix_live_trades_is_open", table_name="live_trades")
    op.drop_index("ix_live_trades_status", table_name="live_trades")
    op.drop_index("ix_live_trades_symbol", table_name="live_trades")
    op.drop_index("ix_live_trades_channel_id", table_name="live_trades")
    op.drop_index("ix_live_trades_signal_id", table_name="live_trades")
    op.drop_index("ix_live_trades_session_id", table_name="live_trades")
    op.drop_index("ix_live_trades_trade_id", table_name="live_trades")
    op.drop_table("live_trades")

    op.drop_index("ix_live_sessions_last_update_at", table_name="live_sessions")
    op.drop_index("ix_live_sessions_started_at", table_name="live_sessions")
    op.drop_index("ix_live_sessions_primary_channel_id", table_name="live_sessions")
    op.drop_index("ix_live_sessions_trading_mode", table_name="live_sessions")
    op.drop_index("ix_live_sessions_status", table_name="live_sessions")
    op.drop_index("ix_live_sessions_session_id", table_name="live_sessions")
    op.drop_table("live_sessions")
