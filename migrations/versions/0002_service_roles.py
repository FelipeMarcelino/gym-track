"""0002 — per-service database roles and least-privilege grants (Q145)

Least privilege between infrastructure services, not just between agents. Each
process logs in as itself, so a bug in the dispatcher cannot rewrite `messages`
and a bug in the API cannot fabricate replies.

The policy itself lives in `app.infrastructure.postgres.grants`, and applying it
lives in `app.infrastructure.postgres.provisioning`. This revision only calls
that code, because roles and grants change on a different clock than the schema:
a rotated password or an edited grant must be able to reach a database that was
stamped with this revision long ago. `make provision` is that path.

Revision ID: 0002_service_roles
Revises: 0001_walking_skeleton
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.config import load_settings
from app.infrastructure.postgres.provisioning import drop_service_roles, sync_service_roles

revision: str = "0002_service_roles"
down_revision: str | None = "0001_walking_skeleton"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sync_service_roles(op.get_bind(), load_settings())


def downgrade() -> None:
    drop_service_roles(op.get_bind(), load_settings())
