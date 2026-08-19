"""0003 — media references and one active conversation per user

Two gaps found in review of WS-7:

* An accepted audio message kept no reference to the provider's media object,
  so speech-to-text would have had nothing to fetch after the webhook returned.
* Nothing stopped two concurrent deliveries from each rotating a timed-out
  conversation, which would split one user's fragments across two threads. The
  partial unique index makes that a database invariant rather than a hope.

Revision ID: 0003_media_and_conversations
Revises: 0002_service_roles

Alembic stores the revision id in a varchar(32), so identifiers stay short --
a longer one fails at stamp time, after the migration has already run.
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_media_and_conversations"
down_revision: str | None = "0002_service_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("provider_media_id", sa.String(length=255), nullable=True))
    op.create_index(
        "uq_conversations_one_active_per_user",
        "conversations",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_conversations_one_active_per_user", table_name="conversations")
    op.drop_column("messages", "provider_media_id")
