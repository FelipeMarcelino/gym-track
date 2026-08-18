"""Applies the role and grant policy to a database (Q145).

Separated from migration 0002 on purpose. A migration runs once per database
and is then stamped forever, which is the wrong shape for two things that
change independently of the schema:

* a rotated password, which must reach PostgreSQL or every connection for that
  service starts failing;
* an edited grant, which would otherwise apply to freshly provisioned
  databases and silently skip the ones already stamped.

So this is written to be re-runnable and convergent: it states the desired end
state and applies it. Migration 0002 calls it to provision a new database, and
`make provision` calls it to reconcile an existing one.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import ApplicationSettings, ServiceName
from app.infrastructure.postgres.grants import SERVICE_GRANTS

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _identifier(name: str) -> str:
    """Reject anything that is not a plain lowercase SQL identifier.

    Role and table names are interpolated into DDL, where bind parameters do
    not exist. Restricting the alphabet is what keeps that safe; quoting alone
    would still allow surprises in a name nobody intended to allow.
    """
    if not _IDENTIFIER.match(name):
        raise ValueError(f"{name!r} is not a safe SQL identifier")
    return name


def _quote_literal(connection: Connection, value: str) -> str:
    """Let PostgreSQL escape the value, through a bind parameter.

    Escaping by hand is how a password containing `$$` ends up terminating a
    surrounding DO block, or a quote ends up terminating the literal. The
    server already knows how to do this correctly.
    """
    return str(
        connection.execute(sa.text("SELECT quote_literal(:value)"), {"value": value}).scalar_one()
    )


def _role_exists(connection: Connection, role: str) -> bool:
    found = connection.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": role}
    ).first()
    return found is not None


def sync_service_roles(connection: Connection, settings: ApplicationSettings) -> None:
    """Create or update every service role and reset its grants to the policy.

    Idempotent by construction: privileges are revoked before being granted, so
    a privilege removed from :data:`SERVICE_GRANTS` disappears on the next run
    instead of lingering on databases that were provisioned earlier.
    """
    for service in ServiceName:
        role = settings.postgres.roles[service]
        name = _identifier(role.user)
        password = _quote_literal(connection, role.password.get_secret_value())

        verb = "ALTER" if _role_exists(connection, name) else "CREATE"
        connection.execute(sa.text(f"{verb} ROLE {name} LOGIN PASSWORD {password}"))

        connection.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {name}"))
        connection.execute(sa.text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {name}"))

        for table, privileges in SERVICE_GRANTS[service].items():
            granted = ", ".join(_privilege(privilege) for privilege in privileges)
            connection.execute(sa.text(f"GRANT {granted} ON {_identifier(table)} TO {name}"))


def drop_service_roles(connection: Connection, settings: ApplicationSettings) -> None:
    for service in ServiceName:
        name = _identifier(settings.postgres.roles[service].user)
        if not _role_exists(connection, name):
            continue
        connection.execute(sa.text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {name}"))
        connection.execute(sa.text(f"REVOKE ALL ON SCHEMA public FROM {name}"))
        connection.execute(sa.text(f"DROP OWNED BY {name}"))
        connection.execute(sa.text(f"DROP ROLE {name}"))


_ALLOWED_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})


def _privilege(privilege: str) -> str:
    if privilege not in _ALLOWED_PRIVILEGES:
        raise ValueError(f"unknown privilege {privilege!r}")
    return privilege


def main() -> None:  # pragma: no cover - thin entrypoint, exercised via sync_service_roles
    """Reconcile an existing database with the current policy."""
    from sqlalchemy import create_engine

    from app.config import load_settings

    settings = load_settings()
    # The synchronous driver keeps this usable from a plain shell command.
    engine = create_engine(settings.postgres.admin_dsn().replace("+asyncpg", "+psycopg"))
    try:
        with engine.begin() as connection:
            sync_service_roles(connection, settings)
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    main()
