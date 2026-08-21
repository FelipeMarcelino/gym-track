"""0012 — the paused run is found by thread, not by namespace (§11.5, Q123)

WS-2 gave `pending_clarifications` a `checkpoint_ns`, following the sprint
plan's D4: two runs of one conversation were to be kept apart by LangGraph's
`checkpoint_ns` while `thread_id` stayed the conversation id.

Measured in WS-4 against the pinned version, that does not work. A run started
under a top-level `checkpoint_ns` writes its checkpoints, but `aget_state`
refuses to read them back -- LangGraph treats the field as a subgraph
namespace. WS-9 has to read and resume exactly that state, so the isolation
moved into a composite `thread_id` of conversation, execution and delivery.

The column follows the fact. Nothing writes it yet, so this is a rename rather
than a migration of data, and leaving the old name would have WS-8 storing a
thread id in a field called a namespace.

Revision ID: 0012_checkpoint_thread_id
Revises: 0011_workflow_operations
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_checkpoint_thread_id"
down_revision: str | None = "0011_workflow_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Wider as well as renamed: a composite thread is three UUIDs and two
    # separators, which does not fit the namespace-sized column.
    op.alter_column(
        "pending_clarifications",
        "checkpoint_ns",
        new_column_name="checkpoint_thread_id",
        type_=sa.String(length=160),
        existing_type=sa.String(length=128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "pending_clarifications",
        "checkpoint_thread_id",
        new_column_name="checkpoint_ns",
        type_=sa.String(length=128),
        existing_type=sa.String(length=160),
        existing_nullable=False,
    )
