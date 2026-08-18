"""0002 — per-service database roles and least-privilege grants (Q145)

Least privilege between infrastructure services, not just between agents. Each
process logs in as itself, so a bug in the dispatcher cannot rewrite `messages`
and a bug in the API cannot fabricate replies.

Two invariants are expressed here and asserted by tests:

* No role is granted DELETE on anything. Removal is soft (§26).
* No role is granted UPDATE or DELETE on `domain_events`: it is append-only,
  and the database is what makes that true rather than a convention.

Roles are created here because that is what makes the grants testable end to
end. A deployment that manages roles outside migrations can run this revision
with the roles already present -- creation is conditional.

Revision ID: 0002_service_roles
Revises: 0001_walking_skeleton
Create Date: 2026-08-18
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from alembic import op

from app.config import ServiceName, load_settings
from app.infrastructure.postgres.grants import SERVICE_GRANTS

revision: str = "0002_service_roles"
down_revision: str | None = "0001_walking_skeleton"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _role_names() -> dict[ServiceName, str]:
    settings = load_settings()
    names = {service: settings.postgres.roles[service].user for service in ServiceName}
    for name in names.values():
        if not _IDENTIFIER.match(name):
            raise ValueError(f"role name {name!r} is not a safe SQL identifier")
    return names


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    settings = load_settings()
    names = _role_names()

    for service, role in names.items():
        password = _quote_literal(settings.postgres.roles[service].password.get_secret_value())
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} LOGIN PASSWORD {password};
                ELSE
                    ALTER ROLE {role} LOGIN PASSWORD {password};
                END IF;
            END
            $$;
            """
        )
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        # Start from nothing, so re-running this revision cannot leave a
        # privilege behind that a later edit meant to remove.
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}")

        for table, privileges in SERVICE_GRANTS[service].items():
            op.execute(f"GRANT {', '.join(privileges)} ON {table} TO {role}")


def downgrade() -> None:
    for role in _role_names().values():
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}")
        op.execute(f"REVOKE ALL ON SCHEMA public FROM {role}")
        op.execute(f"DROP OWNED BY {role}")
        op.execute(f"DROP ROLE IF EXISTS {role}")
