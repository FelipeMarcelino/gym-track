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
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint

from app.config import ApplicationSettings, ServiceName
from app.infrastructure.langgraph.checkpointer import (
    CHECKPOINT_SCHEMA,
    CHECKPOINT_TABLES,
    CheckpointerProvider,
    checkpointer_dsn,
    setup_checkpoint_tables,
)
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
        cursor = await connection.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_name = ANY(%s)",
            (list(CHECKPOINT_TABLES),),
        )
        found = {(schema, table) for schema, table in await cursor.fetchall()}

    assert found == {(CHECKPOINT_SCHEMA, table) for table in CHECKPOINT_TABLES}
    assert not any(schema == "public" for schema, _ in found)


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
