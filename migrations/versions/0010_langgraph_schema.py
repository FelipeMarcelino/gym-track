"""0010 — a schema of its own for LangGraph's checkpoints (§11.5, Q124)

No tables. The checkpointer creates its own four, and it does so from
`make migrate` under the admin identity rather than from a worker, because they
arrive by `CREATE TABLE` and a service role with DDL is a hole in Q145.

What this revision owns is the *boundary*: the schema those tables land in, and
the grants that let exactly one role write there. The grant policy itself lives
in `app.infrastructure.postgres.grants` and applying it lives in
`provisioning`, the same way migration 0002 works -- a grant edited later must
be able to reach a database stamped with this revision long ago, and
`make provision` is that path.

Revision ID: 0010_langgraph_schema
Revises: 0009_plural_aliases
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.config import load_settings
from app.infrastructure.langgraph.checkpointer import CHECKPOINT_SCHEMA
from app.infrastructure.postgres.provisioning import sync_service_roles

revision: str = "0010_langgraph_schema"
down_revision: str | None = "0009_plural_aliases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {CHECKPOINT_SCHEMA}"))
    # Convergent: this is what applies USAGE and the default privileges that
    # make the tables writable when they are created a moment later.
    sync_service_roles(op.get_bind(), load_settings())


def downgrade() -> None:
    # CASCADE, and without regret: checkpoints are rebuildable coordination
    # state, not history. Nothing in `public` references them.
    op.execute(sa.text(f"DROP SCHEMA IF EXISTS {CHECKPOINT_SCHEMA} CASCADE"))
