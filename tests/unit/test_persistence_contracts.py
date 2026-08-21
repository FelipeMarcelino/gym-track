"""WS-3: the persistence contracts that hold without a database running.

Everything here reads the mapped metadata and the grant table. It is the half
of WS-3 that can fail fast on every PR; the half that needs a real PostgreSQL
lives in tests/integration/.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.config import ServiceName
from app.infrastructure.postgres import models
from app.infrastructure.postgres.base import Base
from app.infrastructure.postgres.grants import ALL_TABLES
from app.infrastructure.postgres.grants import SERVICE_GRANTS as GRANTS

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_ROOT = REPO_ROOT / "src" / "app" / "domain"

EXPECTED_TABLES = frozenset(
    {
        "users",
        "training_sessions",
        "session_exercises",
        "exercise_sets",
        "exercise_groups",
        "entity_sources",
        "audit_events",
        "exercises",
        "exercise_aliases",
        "muscles",
        "exercise_muscles",
        "equipment",
        "exercise_equipment",
        "exercise_relations",
        "user_identifiers",
        "conversations",
        "messages",
        "message_batches",
        "message_batch_items",
        "workflow_executions",
        "execution_tasks",
        "pending_clarifications",
        "processed_operations",
        "outbound_messages",
        "domain_events",
        "outbox_events",
    }
)

#: Append-only by §26: nothing may rewrite history after the fact.
APPEND_ONLY_TABLES = frozenset({"domain_events", "audit_events", "entity_sources"})


def test_migration_0001_covers_exactly_the_sprint_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert set(ALL_TABLES) == EXPECTED_TABLES


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("messages", ("provider", "external_message_id")),
        ("user_identifiers", ("provider", "external_id_lookup_hmac")),
        ("outbound_messages", ("response_group_id", "sequence")),
        ("workflow_executions", ("message_batch_id",)),
        ("processed_operations", ("operation_id",)),
        ("outbox_events", ("domain_event_id",)),
        ("message_batch_items", ("message_id",)),
        ("message_batch_items", ("message_batch_id", "position")),
    ],
)
def test_idempotency_keys_are_unique_constraints(table: str, columns: tuple[str, ...]) -> None:
    """§28's keys must be enforced by the database, not by hopeful code."""
    constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables[table].constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert columns in constraints, f"{table} is missing UNIQUE{columns}"


@pytest.mark.parametrize(
    ("table", "column"),
    [
        # §26.2 asks "given this message, what did we write because of it".
        # PostgreSQL does not index a foreign key on its own, so without these
        # the answer is a sequential scan that grows with the whole history.
        ("entity_sources", "message_id"),
        ("entity_sources", "message_batch_id"),
    ],
)
def test_provenance_lookups_are_indexed(table: str, column: str) -> None:
    indexed = {
        next(indexed_column.name for indexed_column in index.columns)
        for index in Base.metadata.tables[table].indexes
    }
    assert column in indexed, f"{table}.{column} has no index leading with it"


def test_every_table_has_a_uuid_primary_key_and_timestamps() -> None:
    for name, table in Base.metadata.tables.items():
        primary_key = list(table.primary_key.columns)
        assert [column.name for column in primary_key] == ["id"], name
        assert isinstance(primary_key[0].type, sa.Uuid), name
        assert {"created_at", "updated_at"} <= set(table.columns.keys()), name


def test_soft_delete_is_carried_only_by_entities_that_keep_history() -> None:
    soft_deletable = {
        name for name, table in Base.metadata.tables.items() if "deleted_at" in table.columns
    }
    assert soft_deletable == {
        "users",
        "conversations",
        "exercises",
        "exercise_aliases",
        "training_sessions",
        "session_exercises",
        "exercise_sets",
        "exercise_groups",
    }


def test_enums_are_checked_strings_rather_than_native_types() -> None:
    """Native enum types cost a migration per added value; a CHECK does not."""
    enum_columns = [
        (table.name, column.name, column.type)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, sa.Enum)
    ]
    assert enum_columns, "the schema is expected to map several enums"
    for table_name, column_name, column_type in enum_columns:
        assert not column_type.native_enum, f"{table_name}.{column_name} uses a native enum"


def test_delivery_states_are_ordered_for_the_dispatcher() -> None:
    """WS-10 asserts transitions never move backwards; the order lives here."""
    assert list(models.DeliveryState) == [
        models.DeliveryState.PENDING,
        models.DeliveryState.DISPATCHING,
        models.DeliveryState.DISPATCHED,
        models.DeliveryState.DELIVERED,
        models.DeliveryState.FAILED,
    ]


# --------------------------------------------------------------------------
# Least privilege (Q145)
# --------------------------------------------------------------------------


def test_every_service_has_a_grant_set() -> None:
    assert set(GRANTS) == set(ServiceName)


def test_grants_only_mention_tables_that_exist() -> None:
    for service, tables in GRANTS.items():
        unknown = set(tables) - EXPECTED_TABLES
        assert not unknown, f"{service.value} is granted on unknown tables: {unknown}"


def test_no_service_may_delete_anything() -> None:
    """Removal is soft (§26), so DELETE is nobody's privilege."""
    for service, tables in GRANTS.items():
        for table, privileges in tables.items():
            assert "DELETE" not in privileges, f"{service.value} may delete from {table}"


def test_append_only_tables_are_never_writable_after_insert() -> None:
    for service, tables in GRANTS.items():
        for table in APPEND_ONLY_TABLES:
            privileges = tables.get(table, ())
            assert "UPDATE" not in privileges, f"{service.value} may rewrite {table}"


@pytest.mark.parametrize(
    ("service", "table"),
    [
        # The two denials the sprint plan calls out by name.
        (ServiceName.DISPATCHER, "messages"),
        (ServiceName.API, "outbound_messages"),
        # An outbox publisher that can touch business tables is not a publisher.
        (ServiceName.OUTBOX_PUBLISHER, "users"),
        (ServiceName.OUTBOX_PUBLISHER, "outbound_messages"),
    ],
)
def test_service_is_denied_the_writes_it_should_not_have(service: ServiceName, table: str) -> None:
    privileges = GRANTS[service].get(table, ())
    assert not ({"INSERT", "UPDATE", "DELETE"} & set(privileges)), (
        f"{service.value} must not write {table}"
    )


def test_message_aggregator_owns_its_batch_writes() -> None:
    """It writes batches in WS-8, so it needs its own role rather than the API's."""
    aggregator = GRANTS[ServiceName.MESSAGE_AGGREGATOR]
    assert "INSERT" in aggregator["message_batches"]
    assert "INSERT" in aggregator["message_batch_items"]
    assert "INSERT" not in GRANTS[ServiceName.API].get("message_batches", ())


# --------------------------------------------------------------------------
# Layering
# --------------------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    return imported


def test_domain_layer_does_not_know_about_sqlalchemy() -> None:
    """The domain must stay portable: persistence is an adapter concern (Q155)."""
    offenders = {
        str(path.relative_to(REPO_ROOT)): sorted(
            module
            for module in _imported_modules(path)
            if module.split(".")[0] in {"sqlalchemy", "alembic", "asyncpg"}
        )
        for path in DOMAIN_ROOT.rglob("*.py")
    }
    leaking = {path: modules for path, modules in offenders.items() if modules}
    assert not leaking, f"domain modules importing persistence: {leaking}"


def test_enum_columns_carry_a_check_constraint() -> None:
    """`native_enum=False` alone yields a bare VARCHAR: the CHECK is what makes
    the string-backed enum a real constraint for writes that bypass the ORM."""
    enum_columns = [
        (table.name, column.name, column.type)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, sa.Enum)
    ]
    for table_name, column_name, column_type in enum_columns:
        assert column_type.create_constraint, f"{table_name}.{column_name} would accept any string"


def test_enum_check_constraints_reach_the_generated_ddl() -> None:
    """The constraint must survive into CREATE TABLE, not just live in metadata."""
    ddl = str(
        sa.schema.CreateTable(Base.metadata.tables["outbound_messages"]).compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )
    assert "CHECK (delivery_state IN (" in ddl
