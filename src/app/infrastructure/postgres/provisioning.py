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

import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import ApplicationSettings, ServiceName
from app.infrastructure.postgres.grants import SCHEMA_GRANTS, SERVICE_GRANTS
from app.infrastructure.postgres.schemas import MANAGED_SCHEMAS

logger = logging.getLogger(__name__)

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


def existing_tables(connection: Connection) -> frozenset[str]:
    """The tables that exist right now, in the public schema."""
    rows = connection.execute(
        sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    ).scalars()
    return frozenset(rows)


def _schema_exists(connection: Connection, schema: str) -> bool:
    found = connection.execute(
        sa.text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :name"),
        {"name": schema},
    ).first()
    return found is not None


def _revoke_schema(connection: Connection, schema: str, role: str) -> None:
    """Take everything back, including what future tables would have inherited.

    `ALTER DEFAULT PRIVILEGES ... GRANT` only *adds* to the default ACL, so a
    privilege dropped from the policy would keep appearing on every table the
    checkpointer creates from then on. Revoking first is what makes the policy
    a policy rather than a high-water mark.
    """
    name = _identifier(schema)
    connection.execute(
        sa.text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {name} REVOKE ALL ON TABLES FROM {role}")
    )
    connection.execute(sa.text(f"REVOKE ALL ON ALL TABLES IN SCHEMA {name} FROM {role}"))
    connection.execute(sa.text(f"REVOKE ALL ON SCHEMA {name} FROM {role}"))


def _sync_schema_grants(connection: Connection, service: ServiceName, role: str) -> None:
    """Grants on schemas outside `public`, applied convergently.

    Every managed schema is revoked first, not only the ones this service is
    granted today: a schema deleted from `SCHEMA_GRANTS` is never visited by a
    loop over the policy, so its privileges would outlive the decision to
    remove them.

    `ALTER DEFAULT PRIVILEGES` is the half that earns its keep. The checkpoint
    tables are created after this runs -- by `make migrate`, not by a migration
    -- and a checkpointer release that adds a fifth table must not need a
    migration before the worker can write to it.
    """
    desired = SCHEMA_GRANTS.get(service, {})
    for schema in MANAGED_SCHEMAS:
        if not _schema_exists(connection, schema):
            logger.info(
                "skipping grants for a schema that does not exist yet",
                extra={"schema": schema, "role": role},
            )
            continue

        _revoke_schema(connection, schema, role)

        privileges = desired.get(schema)
        if privileges is None:
            continue

        name = _identifier(schema)
        granted = ", ".join(_privilege(privilege) for privilege in privileges)
        connection.execute(sa.text(f"GRANT USAGE ON SCHEMA {name} TO {role}"))
        connection.execute(sa.text(f"GRANT {granted} ON ALL TABLES IN SCHEMA {name} TO {role}"))
        connection.execute(
            sa.text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {name} GRANT {granted} ON TABLES TO {role}"
            )
        )


def sync_service_roles(connection: Connection, settings: ApplicationSettings) -> None:
    """Create or update every service role and reset its grants to the policy.

    Idempotent by construction: privileges are revoked before being granted, so
    a privilege removed from :data:`SERVICE_GRANTS` disappears on the next run
    instead of lingering on databases that were provisioned earlier.

    Tables the policy mentions but the database does not have yet are skipped.
    Migration 0002 runs long before the tables later migrations create, and
    granting on a table that does not exist aborts the whole upgrade -- so each
    migration that adds tables calls this again, and the grants converge.
    """
    present = existing_tables(connection)
    for service in ServiceName:
        role = settings.postgres.roles[service]
        name = _identifier(role.user)
        password = _quote_literal(connection, role.password.get_secret_value())

        verb = "ALTER" if _role_exists(connection, name) else "CREATE"
        connection.execute(sa.text(f"{verb} ROLE {name} LOGIN PASSWORD {password}"))

        connection.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {name}"))
        connection.execute(sa.text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {name}"))

        for table, privileges in SERVICE_GRANTS[service].items():
            if table not in present:
                logger.info(
                    "skipping a grant for a table that does not exist yet",
                    extra={"table": table, "role": name},
                )
                continue
            granted = ", ".join(_privilege(privilege) for privilege in privileges)
            connection.execute(sa.text(f"GRANT {granted} ON {_identifier(table)} TO {name}"))

        _sync_schema_grants(connection, service, name)


def drop_service_roles(connection: Connection, settings: ApplicationSettings) -> None:
    for service in ServiceName:
        name = _identifier(settings.postgres.roles[service].user)
        if not _role_exists(connection, name):
            continue
        for schema in MANAGED_SCHEMAS:
            if _schema_exists(connection, schema):
                _revoke_schema(connection, schema, name)
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
