"""0009 — the plurals people count in

"Fiz 10 flexões", never "fiz 10 flexão". Normalization cannot derive this:
"flexões" normalizes to "flexoes", which is a different string from "flexao",
and the fuzzy stage scores the pair at 0.77 — offered rather than written, per
ADR-012. So the plural is an alias, for the handful of exercises people
actually count out loud.

Found by the Sprint 2 closeout: the Definition of Done names "10 flexões" and
the test that claimed to check it used the singular.

Revision ID: 0009_plural_aliases
Revises: 0008_set_notes
Create Date: 2026-08-20 05:39:12.151808+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.infrastructure.postgres.seeding import seed_catalog_sync

revision: str = "0009_plural_aliases"
down_revision: str | None = "0008_set_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No DDL: the catalog gained aliases, and the seed is convergent by design
    # (WS-1). Re-running it adds the new rows, leaves the existing ones alone
    # and touches nothing a user taught us.
    seed_catalog_sync(op.get_bind())


def downgrade() -> None:
    # Re-running the seed against the previous catalog would retire the new
    # aliases, but a downgrade cannot know which catalog it is going back to.
    # Aliases are additive and harmless; leaving them is the honest no-op.
    pass
