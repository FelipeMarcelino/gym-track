"""WS-1: the checkpoint store is real, isolated, and writable by exactly one role.

Q124 asks for checkpoint storage isolated from the domain tables by a schema or
database boundary. The checkpointer offers no schema parameter -- measured, not
assumed -- so the isolation comes from `search_path` on its own connection, and
the first test here is what verifies that rather than trusting it.

The role tests matter for the same reason the rest of Q145 does: the process
that writes checkpoints must not be able to create tables next to them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import psycopg
import pytest
import pytest_asyncio
import sqlalchemy as sa
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint

from app.config import ApplicationSettings, ServiceName
from app.infrastructure.langgraph.checkpointer import (
    CHECKPOINT_TABLES,
    CheckpointerProvider,
    checkpointer_dsn,
    setup_checkpoint_tables,
)
from app.infrastructure.postgres.schemas import CHECKPOINT_SCHEMA
from tests.conftest import requires_docker

pytestmark = [requires_docker, pytest.mark.asyncio]

METADATA = CheckpointMetadata(source="input", step=-1, parents={})


def _admin_dsn(settings: ApplicationSettings) -> str:
    """The admin identity, scoped to the checkpoint schema like the real one."""
    return checkpointer_dsn(settings.postgres, settings.postgres.admin)


def _unscoped_admin_dsn(settings: ApplicationSettings) -> str:
    """The same identity without the search_path, for looking at the database
    the way every other process sees it."""
    return checkpointer_dsn(settings.postgres, settings.postgres.admin, search_path=None)


def _worker_dsn(settings: ApplicationSettings) -> str:
    return checkpointer_dsn(settings.postgres, settings.postgres.roles[ServiceName.WORKFLOW_WORKER])


@pytest_asyncio.fixture
async def checkpoint_store(
    migrated_database: ApplicationSettings,
) -> AsyncIterator[ApplicationSettings]:
    """The tables `make migrate` creates, created the same way here."""
    await setup_checkpoint_tables(
        checkpointer_dsn(migrated_database.postgres, migrated_database.postgres.admin)
    )
    yield migrated_database


def _config(thread: str, namespace: str = "") -> RunnableConfig:
    return {"configurable": {"thread_id": thread, "checkpoint_ns": namespace}}


async def test_the_checkpoint_tables_live_in_their_own_schema(
    checkpoint_store: ApplicationSettings,
) -> None:
    """The measurement D2 rests on, made against the pinned version.

    If this fails, the checkpointer is writing next to the domain tables and
    Q124's boundary exists only in the documentation.
    """
    async with await psycopg.AsyncConnection.connect(
        _unscoped_admin_dsn(checkpoint_store)
    ) as connection:
        # Every table in the schema, not only the ones we expect: a release
        # that adds a fifth must fail here rather than land somewhere the
        # grants and this module's constant do not know about.
        cursor = await connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (CHECKPOINT_SCHEMA,),
        )
        in_schema = {name for (name,) in await cursor.fetchall()}

        cursor = await connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (list(CHECKPOINT_TABLES),),
        )
        in_public = {name for (name,) in await cursor.fetchall()}

    assert in_schema == set(CHECKPOINT_TABLES)
    assert in_public == set()


async def test_a_checkpoint_survives_the_process_that_wrote_it(
    checkpoint_store: ApplicationSettings,
) -> None:
    """The whole reason the checkpointer is in PostgreSQL rather than memory.

    A restart between a question and its answer must not lose the workflow, so
    the second provider is built from scratch -- new pool, new connections --
    and has to see what the first one wrote.
    """
    config = _config("thread-survives")

    async with CheckpointerProvider(_admin_dsn(checkpoint_store)) as saver:
        written = await saver.aput(config, empty_checkpoint(), METADATA, {})

    async with CheckpointerProvider(_admin_dsn(checkpoint_store)) as saver:
        read_back = await saver.aget_tuple(written)

    assert read_back is not None
    assert read_back.config["configurable"]["thread_id"] == "thread-survives"


async def test_the_worker_role_can_write_a_checkpoint(
    checkpoint_store: ApplicationSettings,
) -> None:
    """DML, under the identity the workflow worker actually connects with."""
    config = _config("thread-worker-writes")

    async with CheckpointerProvider(_worker_dsn(checkpoint_store)) as saver:
        written = await saver.aput(config, empty_checkpoint(), METADATA, {})
        assert await saver.aget_tuple(written) is not None


async def test_the_worker_role_cannot_create_a_table_there(
    checkpoint_store: ApplicationSettings,
) -> None:
    """DML yes, DDL never (Q145).

    On its own connection: a statement refused inside an already-aborted
    transaction fails for the wrong reason, and a test that cannot tell the two
    apart is not testing permissions.
    """
    async with await psycopg.AsyncConnection.connect(_worker_dsn(checkpoint_store)) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await connection.execute(f"CREATE TABLE {CHECKPOINT_SCHEMA}.intruder (id int)")


async def test_a_table_created_later_is_writable_without_a_migration(
    checkpoint_store: ApplicationSettings,
) -> None:
    """What ALTER DEFAULT PRIVILEGES buys.

    A checkpointer release that adds a fifth table must not need a migration
    before the worker can write to it -- otherwise the upgrade that creates it
    is the upgrade that breaks the worker.
    """
    async with await psycopg.AsyncConnection.connect(
        _admin_dsn(checkpoint_store), autocommit=True
    ) as connection:
        await connection.execute(f"CREATE TABLE {CHECKPOINT_SCHEMA}.checkpoint_later (id int)")

    try:
        async with await psycopg.AsyncConnection.connect(
            _worker_dsn(checkpoint_store), autocommit=True
        ) as connection:
            await connection.execute("INSERT INTO checkpoint_later (id) VALUES (1)")
            cursor = await connection.execute("SELECT count(*) FROM checkpoint_later")
            row = await cursor.fetchone()
            assert row is not None and row[0] == 1
    finally:
        async with await psycopg.AsyncConnection.connect(
            _admin_dsn(checkpoint_store), autocommit=True
        ) as connection:
            await connection.execute(f"DROP TABLE {CHECKPOINT_SCHEMA}.checkpoint_later")


async def test_a_privilege_removed_from_the_policy_stops_reaching_new_tables(
    checkpoint_store: ApplicationSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy has to be able to take something away, not only add.

    `ALTER DEFAULT PRIVILEGES ... GRANT` accumulates, so without a revoke first
    a privilege dropped from `SCHEMA_GRANTS` would keep landing on every table
    the checkpointer creates afterwards -- the grants would drift permanently
    away from the policy that is supposed to describe them.
    """
    from sqlalchemy import create_engine

    from app.infrastructure.postgres import grants, provisioning

    reduced = {
        ServiceName.WORKFLOW_WORKER: {CHECKPOINT_SCHEMA: ("SELECT",)},
    }
    monkeypatch.setattr(provisioning, "SCHEMA_GRANTS", reduced)
    monkeypatch.setattr(grants, "SCHEMA_GRANTS", reduced)

    engine = create_engine(checkpoint_store.postgres.admin_dsn().replace("+asyncpg", "+psycopg"))
    try:
        with engine.begin() as connection:
            provisioning.sync_service_roles(connection, checkpoint_store)
        with engine.begin() as connection:
            connection.execute(
                sa.text(f"CREATE TABLE {CHECKPOINT_SCHEMA}.checkpoint_reduced (id int)")
            )

        worker = checkpoint_store.postgres.roles[ServiceName.WORKFLOW_WORKER].user
        with engine.connect() as connection:
            allowed = connection.execute(
                sa.text(
                    "SELECT privilege_type FROM information_schema.table_privileges "
                    "WHERE table_schema = :schema AND table_name = 'checkpoint_reduced' "
                    "AND grantee = :role"
                ),
                {"schema": CHECKPOINT_SCHEMA, "role": worker},
            ).scalars()
            assert set(allowed) == {"SELECT"}
    finally:
        # Undo the patch *before* reprovisioning. pytest restores the attribute
        # after the test returns, so a `sync_service_roles` call inside this
        # block still sees the reduced policy and would leave the worker role
        # SELECT-only for the rest of the session -- database state that no
        # fixture teardown reverses, and that surfaces as an unrelated test
        # failing with insufficient privileges.
        monkeypatch.undo()
        with engine.begin() as connection:
            connection.execute(
                sa.text(f"DROP TABLE IF EXISTS {CHECKPOINT_SCHEMA}.checkpoint_reduced")
            )
            provisioning.sync_service_roles(connection, checkpoint_store)
        engine.dispose()


async def test_the_reduced_policy_test_puts_the_grants_back(
    checkpoint_store: ApplicationSettings,
) -> None:
    """The guard for the test above.

    Reprovisioning writes database state, and no fixture teardown reverses it.
    If the restore ever stops working, the symptom is an unrelated test failing
    on privileges much later in the run -- so it is asserted here instead.
    """
    worker = checkpoint_store.postgres.roles[ServiceName.WORKFLOW_WORKER]

    async with await psycopg.AsyncConnection.connect(
        _unscoped_admin_dsn(checkpoint_store), autocommit=True
    ) as connection:
        cursor = await connection.execute(
            "SELECT DISTINCT privilege_type FROM information_schema.table_privileges "
            "WHERE table_schema = %s AND grantee = %s",
            (CHECKPOINT_SCHEMA, worker.user),
        )
        allowed = {privilege for (privilege,) in await cursor.fetchall()}

    assert allowed == {"SELECT", "INSERT", "UPDATE", "DELETE"}


async def test_the_search_path_is_what_isolates_it(
    checkpoint_store: ApplicationSettings,
) -> None:
    """The mechanism, asserted directly.

    Without the search_path the same unqualified name resolves to nothing in
    `public` -- which is the proof that nothing was quietly created there.
    """
    bare = _unscoped_admin_dsn(checkpoint_store)
    async with await psycopg.AsyncConnection.connect(bare, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.UndefinedTable):
            await connection.execute("SELECT 1 FROM checkpoints")
