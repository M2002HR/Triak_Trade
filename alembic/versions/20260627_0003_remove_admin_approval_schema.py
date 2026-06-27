"""remove admin approval schema

Revision ID: 20260627_0003
Revises: 20260627_0002
Create Date: 2026-06-27 00:30:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260627_0003"
down_revision = "20260627_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "admin_decisions" in tables:
        index_names = {index["name"] for index in inspector.get_indexes("admin_decisions")}
        if "ix_admin_decisions_action_id" in index_names:
            op.drop_index("ix_admin_decisions_action_id", table_name="admin_decisions")
        op.drop_table("admin_decisions")

    if "proposed_actions" in tables:
        index_names = {index["name"] for index in inspector.get_indexes("proposed_actions")}
        if "ix_proposed_actions_signal_id" in index_names:
            op.drop_index("ix_proposed_actions_signal_id", table_name="proposed_actions")
        if "ix_proposed_actions_action_id" in index_names:
            op.drop_index("ix_proposed_actions_action_id", table_name="proposed_actions")
        op.drop_table("proposed_actions")


def downgrade() -> None:
    op.create_table(
        "proposed_actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column("risk_increasing", sa.Boolean(), nullable=False),
        sa.Column("requires_admin_approval", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Numeric(10, 6), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("action_id"),
    )
    op.create_index("ix_proposed_actions_action_id", "proposed_actions", ["action_id"])
    op.create_index("ix_proposed_actions_signal_id", "proposed_actions", ["signal_id"])

    op.create_table(
        "admin_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("admin_user_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_decisions_action_id", "admin_decisions", ["action_id"])
