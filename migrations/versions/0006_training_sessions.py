"""0006 — training sessions and the audit trail (§18, §15, §26)

`training_sessions` carries the partial unique index that makes "one open
session per user" a database invariant: two concurrent logs would otherwise
each open one, and a user's sets would land in two workouts. Sprint 1 learned
that lesson on conversations; here it ships before the failure can happen.

`audit_events` lands with this migration rather than with the workout schema
because sessions are the first thing this sprint mutates, and a table that
appears one migration after its first auditable event is a gap nobody notices
until an audit asks about the missing week. It is append-only (§26): no service
role may UPDATE or DELETE it.

Revision ID: 0006_training_sessions
Revises: 0005_exercise_catalog
Create Date: 2026-08-20 00:53:55.670176+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import load_settings
from app.infrastructure.postgres.provisioning import sync_service_roles

revision: str = "0006_training_sessions"
down_revision: str | None = "0005_exercise_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "closed",
                name="trainingsessionstatus",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(length=16), nullable=True),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_training_sessions_expiry",
        "training_sessions",
        ["status", "last_activity_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_training_sessions_user_id"), "training_sessions", ["user_id"], unique=False
    )
    op.create_index(
        "uq_training_sessions_one_active_per_user",
        "training_sessions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
    )
    op.create_table(
        "audit_events",
        sa.Column(
            "actor_type",
            sa.Enum(
                "user",
                "system",
                "operator",
                name="actortype",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_execution_id", sa.Uuid(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"], ["workflow_executions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_actor", "audit_events", ["actor_user_id", "occurred_at"], unique=False
    )
    op.create_index(
        "ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"], unique=False
    )

    # Migration 0002 granted what existed then, and there is a new role as
    # well. Re-running the policy is what keeps grants converged -- the same
    # reason 0005 does it.
    sync_service_roles(op.get_bind(), load_settings())


def downgrade() -> None:
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_index("ix_audit_events_actor", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(
        "uq_training_sessions_one_active_per_user",
        table_name="training_sessions",
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_training_sessions_user_id"), table_name="training_sessions")
    op.drop_index("ix_training_sessions_expiry", table_name="training_sessions")
    op.drop_table("training_sessions")
