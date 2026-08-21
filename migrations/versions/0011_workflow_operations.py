"""0011 — execution tasks and pending clarifications (§11.2, §26, Q118, Q125, Q128)

Two tables and four changes to `workflow_executions`.

The tables exist so a workflow's operational state is answerable in SQL while
it runs and after it ends. A LangGraph checkpoint knows a run is suspended, but
it cannot be *searched* for "does this conversation have an open question",
which is what every incoming message has to ask.

`workflow_executions` gains a wider status vocabulary (Q28), the terminality
CHECK that vocabulary makes necessary, the graph version that produced the row
(Q132), and the link an answer needs back to the execution it resumed.

Revision ID: 0011_workflow_operations
Revises: 0010_langgraph_schema
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.config import load_settings
from app.infrastructure.postgres.provisioning import sync_service_roles

revision: str = "0011_workflow_operations"
down_revision: str | None = "0010_langgraph_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The status vocabulary before and after. Written out rather than derived from
#: the enum: a migration describes the database at a point in time, and one
#: that follows the current code stops being a description of what it did.
_OLD_WORKFLOW_STATUSES = "'running', 'succeeded', 'failed'"
_NEW_WORKFLOW_STATUSES = "'running', 'succeeded', 'failed', 'waiting_for_user', 'partial_success'"
_TERMINAL_WORKFLOW_STATUSES = "'succeeded', 'failed', 'partial_success'"


def upgrade() -> None:
    op.create_table(
        "execution_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("workflow_execution_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=128), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("result_visibility", sa.String(length=64), nullable=False),
        sa.Column("depends_on", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("payload", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result_facts", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"], ["workflow_executions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Written by hand, and it must stay that way: `pending_clarifications`
        # references this pair, and a composite foreign key needs a UNIQUE
        # constraint -- not merely a unique index -- to point at. Sprint 2 lost
        # an afternoon to Alembic omitting exactly this.
        sa.UniqueConstraint(
            "workflow_execution_id", "task_key", name="uq_execution_tasks_key_per_execution"
        ),
        sa.CheckConstraint(
            "task_type IN ('conversation', 'log_workout', 'correction', "
            "'training_analysis', 'recommendation', 'workout_program')",
            name="ck_execution_tasks_task_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'completed', 'failed', "
            "'waiting_for_user', 'skipped')",
            name="ck_execution_tasks_status",
        ),
        sa.CheckConstraint(
            "result_visibility IN ('user_visible', 'internal')",
            name="ck_execution_tasks_result_visibility",
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'failed', 'skipped')) = (finished_at IS NOT NULL)",
            name="ck_execution_tasks_finished_when_terminal",
        ),
    )
    op.create_index(
        op.f("ix_execution_tasks_workflow_execution_id"),
        "execution_tasks",
        ["workflow_execution_id"],
    )

    op.create_table(
        "pending_clarifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("clarification_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_execution_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=128), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer_message_batch_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"], ["workflow_executions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["answer_message_batch_id"], ["message_batches.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id", "task_key"],
            ["execution_tasks.workflow_execution_id", "execution_tasks.task_key"],
            name="fk_pending_clarifications_task_in_same_execution",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clarification_id", name="uq_pending_clarifications_clarification_id"),
        sa.CheckConstraint(
            "reason IN ('missing_essential_data', 'ambiguous_entity')",
            name="ck_pending_clarifications_reason",
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'answered', 'cancelled', 'expired')",
            name="ck_pending_clarifications_status",
        ),
        sa.CheckConstraint(
            "(status = 'waiting') = (resolved_at IS NULL)",
            name="ck_pending_clarifications_resolved_when_closed",
        ),
    )
    op.create_index(
        op.f("ix_pending_clarifications_user_id"), "pending_clarifications", ["user_id"]
    )
    op.create_index("ix_pending_clarifications_expiry", "pending_clarifications", ["expires_at"])
    # Partial: the invariant is on *open* questions, not on how many a
    # conversation may ever have had.
    op.create_index(
        "uq_pending_clarifications_open_per_conversation",
        "pending_clarifications",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'waiting'"),
    )

    # `workflowexecutionstatus` is SQLAlchemy's auto-generated name from
    # migration 0001. Replaced by an explicit one, so the next revision that
    # widens this vocabulary does not have to guess.
    op.drop_constraint("workflowexecutionstatus", "workflow_executions", type_="check")
    op.create_check_constraint(
        "ck_workflow_executions_status",
        "workflow_executions",
        f"status IN ({_NEW_WORKFLOW_STATUSES})",
    )
    op.create_check_constraint(
        "ck_workflow_executions_finished_when_terminal",
        "workflow_executions",
        f"(status IN ({_TERMINAL_WORKFLOW_STATUSES})) = (finished_at IS NOT NULL)",
    )
    op.add_column(
        "workflow_executions", sa.Column("graph_version", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "workflow_executions", sa.Column("resumed_execution_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_workflow_executions_resumed_execution",
        "workflow_executions",
        "workflow_executions",
        ["resumed_execution_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Convergent, like every migration that adds a table: the grants for the
    # new ones are applied here rather than waiting for `make provision`.
    sync_service_roles(op.get_bind(), load_settings())


def downgrade() -> None:
    op.drop_constraint(
        "fk_workflow_executions_resumed_execution", "workflow_executions", type_="foreignkey"
    )
    op.drop_column("workflow_executions", "resumed_execution_id")
    op.drop_column("workflow_executions", "graph_version")
    op.drop_constraint(
        "ck_workflow_executions_finished_when_terminal", "workflow_executions", type_="check"
    )
    op.drop_constraint("ck_workflow_executions_status", "workflow_executions", type_="check")
    # Deliberately fails if a paused or partially-successful execution exists.
    # A downgrade that silently discarded a user's open question would be worse
    # than one that refuses to run.
    op.create_check_constraint(
        "workflowexecutionstatus",
        "workflow_executions",
        f"status IN ({_OLD_WORKFLOW_STATUSES})",
    )

    op.drop_index(
        "uq_pending_clarifications_open_per_conversation", table_name="pending_clarifications"
    )
    op.drop_index("ix_pending_clarifications_expiry", table_name="pending_clarifications")
    op.drop_index(op.f("ix_pending_clarifications_user_id"), table_name="pending_clarifications")
    op.drop_table("pending_clarifications")
    op.drop_index(op.f("ix_execution_tasks_workflow_execution_id"), table_name="execution_tasks")
    op.drop_table("execution_tasks")
