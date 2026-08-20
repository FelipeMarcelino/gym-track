"""Ephemeral real infrastructure for every suite that needs it (Q158).

Kept at the root of `tests/` rather than under `integration/` so the end-to-end
suite shares the same containers: one PostgreSQL, one RabbitMQ and one Redis
per session, however many suites use them.

Containers are session-scoped: the suite runs on every PR, so paying the
startup cost once is the difference between a gate people keep and a gate
people disable.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import ApplicationSettings, ServiceName, load_settings
from app.infrastructure.postgres.engine import create_session_factory

REPO_ROOT = Path(__file__).resolve().parents[1]

ADMIN_USER = "gym_track"
ADMIN_PASSWORD = "integration-test"
DATABASE = "gym_track"

SERVICE_PASSWORD = "integration-test"


def _docker_is_available() -> bool:
    """Ask the CLI, not the library.

    A socket the current user cannot open looks the same to `docker info` as a
    daemon that is not running, and both mean the same thing here: skip.
    """
    try:
        probe = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_is_available(),
    reason="needs a reachable Docker daemon for ephemeral infrastructure (Q158)",
)


@pytest.fixture(scope="session")
def postgres_dsn_parts() -> Iterator[tuple[str, int]]:
    """A PostgreSQL container, alive for the whole session."""
    if not _docker_is_available():
        pytest.skip("no reachable Docker daemon")

    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(
        "postgres:17-alpine",
        username=ADMIN_USER,
        password=ADMIN_PASSWORD,
        dbname=DATABASE,
    )
    with container:
        yield container.get_container_host_ip(), int(container.get_exposed_port(5432))


@pytest.fixture(scope="session")
def settings(postgres_dsn_parts: tuple[str, int]) -> Iterator[ApplicationSettings]:
    """Settings pointed at the container, exported so migrations see them too."""
    host, port = postgres_dsn_parts
    environment = {
        "GYM_TRACK_ENVIRONMENT": "test",
        "GYM_TRACK_POSTGRES__HOST": host,
        "GYM_TRACK_POSTGRES__PORT": str(port),
        "GYM_TRACK_POSTGRES__DATABASE": DATABASE,
        "GYM_TRACK_POSTGRES__ADMIN__USER": ADMIN_USER,
        "GYM_TRACK_SECRET_POSTGRES_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "GYM_TRACK_RABBITMQ__HOST": "localhost",
        "GYM_TRACK_RABBITMQ__USER": "gym_track",
        "GYM_TRACK_SECRET_RABBITMQ_PASSWORD": "integration-test",
        "GYM_TRACK_REDIS__HOST": "localhost",
        "GYM_TRACK_SECRET_SECURITY_WHATSAPP_APP_SECRET": "integration-test",
        "GYM_TRACK_SECRET_SECURITY_BSUID_ENCRYPTION_KEY": "integration-test",
        "GYM_TRACK_SECRET_SECURITY_BSUID_LOOKUP_HMAC_KEY": "integration-test",
    }
    for service in ServiceName:
        slug = service.value.replace("-", "_")
        environment[f"GYM_TRACK_POSTGRES__ROLES__{service.value.upper()}__USER"] = f"gym_{slug}"
        environment[f"GYM_TRACK_SECRET_POSTGRES_{slug.upper()}_PASSWORD"] = SERVICE_PASSWORD

    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    try:
        yield load_settings(_env_file=None)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="session")
def rabbitmq_url() -> Iterator[str]:
    """A RabbitMQ container, alive for the whole session (Q158)."""
    if not _docker_is_available():
        pytest.skip("no reachable Docker daemon")

    from testcontainers.community.rabbitmq import RabbitMqContainer

    container = RabbitMqContainer(
        "rabbitmq:4-alpine", username="gym_track", password="integration-test"
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5672)
        yield f"amqp://gym_track:integration-test@{host}:{port}/"


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """A Redis container, alive for the whole session (Q158)."""
    if not _docker_is_available():
        pytest.skip("no reachable Docker daemon")

    from testcontainers.community.redis import RedisContainer

    container = RedisContainer("redis:7-alpine")
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


async def redeclare_topology(channel: Any, topology: Any) -> None:
    """Declare a topology from scratch, deleting whatever is there first.

    Two suites declare overlapping queue names with different arguments -- the
    retry tiers are milliseconds here and minutes there -- and RabbitMQ refuses
    a redeclaration that does not match, closing the channel. Deleting first
    keeps each suite's declaration authoritative for its own run.
    """
    from app.infrastructure.rabbitmq.connection import declare_topology

    for queue in topology.queues:
        await channel.queue_delete(queue.name)
    await declare_topology(channel, topology)


def alembic_config(settings: ApplicationSettings) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.postgres.admin_dsn())
    return config


@pytest.fixture(scope="session")
def migrated_database(settings: ApplicationSettings) -> Iterator[ApplicationSettings]:
    """Schema and roles, applied exactly the way `make migrate` applies them."""
    config = alembic_config(settings)
    command.upgrade(config, "head")
    try:
        yield settings
    finally:
        command.downgrade(config, "base")


@pytest.fixture
async def admin_engine(migrated_database: ApplicationSettings) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(migrated_database.postgres.admin_dsn())


@pytest.fixture
async def session_factory(
    admin_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(admin_engine)


#: Deletes rather than one TRUNCATE ... CASCADE, because the catalog must
#: survive. `exercise_aliases` references `users`, so a cascading truncate of
#: `users` would take every seeded alias with it and leave the resolver with
#: nothing to match — reference data seeded by migration 0005 is not test state.
#: User-learned aliases *are* test state, so those go by predicate.
CLEANUP_STATEMENTS: tuple[str, ...] = (
    "DELETE FROM audit_events",
    "DELETE FROM entity_sources",
    "DELETE FROM exercise_sets",
    "DELETE FROM session_exercises",
    "DELETE FROM exercise_groups",
    "DELETE FROM outbox_events",
    "DELETE FROM domain_events",
    "DELETE FROM outbound_messages",
    "DELETE FROM processed_operations",
    "DELETE FROM workflow_executions",
    "DELETE FROM message_batch_items",
    "DELETE FROM message_batches",
    "DELETE FROM messages",
    "DELETE FROM training_sessions",
    "DELETE FROM conversations",
    "DELETE FROM user_identifiers",
    "DELETE FROM exercise_aliases WHERE user_id IS NOT NULL",
    "DELETE FROM users",
)


@pytest.fixture(autouse=True)
async def clean_tables(request: pytest.FixtureRequest) -> None:
    """Each test starts from an empty schema, without paying for a new container."""
    if "migrated_database" not in request.fixturenames:
        return
    settings: ApplicationSettings = request.getfixturevalue("migrated_database")
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(settings.postgres.admin_dsn())
    try:
        async with engine.begin() as connection:
            for statement in CLEANUP_STATEMENTS:
                await connection.execute(sa.text(statement))
    finally:
        await engine.dispose()
