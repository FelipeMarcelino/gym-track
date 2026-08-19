"""0004 — a message belongs to at most one batch

Found in review of WS-8. Nothing prevented two flushes from building batches
out of the same fragments, which would have produced two workflow executions
for one interaction. The old per-batch uniqueness only stopped a message from
appearing twice inside the *same* batch.

Revision ID: 0004_one_batch_per_message
Revises: 0003_media_and_conversations
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_one_batch_per_message"
down_revision: str | None = "0003_media_and_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_batch_items_batch_message", "message_batch_items", type_="unique")
    op.create_unique_constraint("uq_batch_items_message", "message_batch_items", ["message_id"])


def downgrade() -> None:
    op.drop_constraint("uq_batch_items_message", "message_batch_items", type_="unique")
    op.create_unique_constraint(
        "uq_batch_items_batch_message",
        "message_batch_items",
        ["message_batch_id", "message_id"],
    )
