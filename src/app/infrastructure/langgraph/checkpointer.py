"""Where a paused workflow lives (§11.5, Q124, Q145).

LangGraph's PostgreSQL checkpointer keeps four tables. Q124 asks for them to be
isolated from the domain tables by a schema or database boundary, and the
package offers **no** way to ask for a schema -- measured against version 3.1.2
rather than recalled: the string does not appear anywhere in it, and `setup()`
issues unqualified `CREATE TABLE` statements. So the boundary comes from the
`search_path` of the connection the checkpointer is handed, and
`tests/integration/test_checkpointer.py` asserts where the tables actually
landed instead of trusting this paragraph.

Two further decisions are worth reading before changing anything here:

* **The pool is psycopg, not the SQLAlchemy engine.** The checkpointer commits
  on its own connection, in its own transaction, and a helper that hid that
  would hide the one seam the workflow worker has to reason about: a checkpoint
  can outlive a domain transaction that rolled back. The database is
  authoritative about what happened; the checkpoint only says where to continue.
* **DDL belongs to the admin identity.** `setup()` creates tables, so it runs
  from `make migrate`, never from a worker. The workflow-worker role gets DML in
  this schema and nothing else -- including, deliberately, DELETE, which it is
  granted nowhere else in the system because a checkpointer that cannot delete
  its own superseded writes grows without bound.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Final
from urllib.parse import quote

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import PostgresRole, PostgresSettings

logger = logging.getLogger(__name__)

#: Isolated from `public` on purpose (Q124). Named in ADR-015.
CHECKPOINT_SCHEMA: Final = "langgraph"

#: What `setup()` creates, read out of the package rather than guessed. The
#: integration suite asserts this set is exactly what appears in the schema, so
#: a version that adds a fifth table fails a test instead of silently landing
#: somewhere ungranted.
CHECKPOINT_TABLES: Final[tuple[str, ...]] = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)


def checkpointer_dsn(
    postgres: PostgresSettings,
    role: PostgresRole,
    *,
    search_path: str | None = CHECKPOINT_SCHEMA,
) -> str:
    """A psycopg DSN for `role`, scoped to the checkpoint schema.

    `search_path` is the isolation mechanism, not a convenience: without it the
    checkpointer's unqualified DDL and DML resolve in `public`, next to the
    domain tables. Passing `None` is for tests that need to look at the
    database the way everything else sees it.
    """
    user = quote(role.user, safe="")
    password = quote(role.password.get_secret_value(), safe="")
    dsn = f"postgresql://{user}:{password}@{postgres.host}:{postgres.port}/{postgres.database}"
    if search_path is None:
        return dsn
    # Percent-encoded because the value travels inside a URL query parameter,
    # where a bare space would end the option and a bare `=` would start
    # another one.
    return f"{dsn}?options={quote(f'-c search_path={search_path}', safe='')}"


async def setup_checkpoint_tables(dsn: str) -> None:
    """Create the checkpoint tables. Idempotent, and run as the DDL owner.

    Idempotent because the checkpointer keeps its own `checkpoint_migrations`
    ledger, which is what makes it safe on every `make migrate` rather than
    only on the first one.
    """
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
    logger.info("checkpoint tables reconciled", extra={"schema": CHECKPOINT_SCHEMA})


class CheckpointerProvider:
    """Owns the psycopg pool for the lifetime of one process (§37.4).

    A pool rather than a connection because the worker's graph runs one
    interaction at a time but the checkpointer opens and releases connections
    around every node; and explicitly closed on the way out, so a SIGTERM does
    not leave sockets to be reaped by the server.
    """

    def __init__(self, dsn: str, *, max_size: int = 4) -> None:
        self._dsn = dsn
        self._max_size = max_size
        self._pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None

    async def __aenter__(self) -> AsyncPostgresSaver:
        pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
            self._dsn,
            max_size=self._max_size,
            open=False,
            connection_class=AsyncConnection[DictRow],
            kwargs={
                # The checkpointer's own statements decide their transactions;
                # a pool that wrapped them in one of ours would make a
                # checkpoint write depend on when we happened to commit.
                "autocommit": True,
                # The saver reads rows by name. `from_conn_string` arranges
                # this for you; a pool you build yourself does not, and the
                # failure would surface as a KeyError deep inside a resume.
                "row_factory": dict_row,
                # Preparing statements a short-lived pooled connection will not
                # reuse only costs round trips.
                "prepare_threshold": 0,
            },
        )
        await pool.open(wait=True)
        self._pool = pool
        return AsyncPostgresSaver(pool)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()


def main() -> None:  # pragma: no cover - thin entrypoint, exercised by the suite
    """Reconcile the checkpoint tables, as the admin identity.

    Invoked by `make migrate` and by the compose `migrate` service -- both,
    because only one of them goes through the Makefile and a fresh volume that
    ran the other would start a worker against an empty schema.
    """
    from app.config import load_settings
    from app.observability import configure_logging

    settings = load_settings()
    configure_logging(settings.observability.log_level)
    dsn = checkpointer_dsn(settings.postgres, settings.postgres.admin)
    asyncio.run(setup_checkpoint_tables(dsn))


if __name__ == "__main__":  # pragma: no cover
    main()
