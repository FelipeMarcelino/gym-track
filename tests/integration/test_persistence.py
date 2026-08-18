"""WS-3 against a real PostgreSQL: the claims that only a database can settle."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import ApplicationSettings, ServiceName
from app.infrastructure.postgres.base import select_active
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.grants import SERVICE_GRANTS
from app.infrastructure.postgres.models import (
    Conversation,
    MessageContentType,
    MessageDirection,
    MessagingProvider,
    User,
)
from tests.integration.conftest import alembic_config

pytestmark = [pytest.mark.integration]


async def test_unit_of_work_commits_on_success(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with unit_of_work(session_factory) as session:
        session.add(User(locale="pt-BR", timezone="America/Sao_Paulo"))

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(User)) == 1


async def test_unit_of_work_rolls_back_on_exception(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class RollbackError(RuntimeError):
        pass

    with pytest.raises(RollbackError):
        async with unit_of_work(session_factory) as session:
            session.add(User(locale="pt-BR", timezone="UTC"))
            await session.flush()
            raise RollbackError

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(User)) == 0


async def test_soft_deleted_rows_leave_the_default_query(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with unit_of_work(session_factory) as session:
        kept = User(locale="pt-BR", timezone="UTC")
        removed = User(locale="pt-BR", timezone="UTC", deleted_at=datetime.now(UTC))
        session.add_all([kept, removed])

    async with session_factory() as session:
        active = (await session.scalars(select_active(User))).all()
        everything = (await session.scalars(sa.select(User))).all()

    assert len(active) == 1
    assert len(everything) == 2, "soft delete must hide the row, not lose it"


async def test_primary_keys_are_monotonic_within_a_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with unit_of_work(session_factory) as session:
        users = [User(locale="pt-BR", timezone="UTC") for _ in range(50)]
        session.add_all(users)
        await session.flush()
        identifiers = [user.id for user in users]

    assert identifiers == sorted(identifiers)


async def test_duplicate_external_message_id_is_refused_by_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§28's inbound key. WS-7 relies on this to make webhook delivery idempotent."""
    from app.infrastructure.postgres.models import Message

    async with unit_of_work(session_factory) as session:
        user = User(locale="pt-BR", timezone="UTC")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        session.add(
            Message(
                user_id=user.id,
                conversation_id=conversation.id,
                provider=MessagingProvider.WHATSAPP,
                external_message_id="wamid.duplicate",
                direction=MessageDirection.INBOUND,
                content_type=MessageContentType.TEXT,
                text="oi",
            )
        )
        stored_user, stored_conversation = user.id, conversation.id

    with pytest.raises(sa.exc.IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(
                Message(
                    user_id=stored_user,
                    conversation_id=stored_conversation,
                    provider=MessagingProvider.WHATSAPP,
                    external_message_id="wamid.duplicate",
                    direction=MessageDirection.INBOUND,
                    content_type=MessageContentType.TEXT,
                    text="oi de novo",
                )
            )


def test_migrations_apply_and_downgrade_cleanly(settings: ApplicationSettings) -> None:
    """A migration that cannot be undone is a migration nobody dares to deploy."""
    config = alembic_config(settings)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")


async def test_migrated_schema_matches_the_models(
    migrated_database: ApplicationSettings,
) -> None:
    """Models and migrations drift apart silently; autogenerate says when."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.infrastructure.postgres.base import Base

    engine = create_async_engine(migrated_database.postgres.admin_dsn())
    try:
        async with engine.connect() as connection:
            differences = await connection.run_sync(
                lambda sync_connection: compare_metadata(
                    MigrationContext.configure(sync_connection), Base.metadata
                )
            )
    finally:
        await engine.dispose()

    assert differences == [], f"schema drifted from the models: {differences}"


# --------------------------------------------------------------------------
# Least privilege, asserted rather than assumed (Q145)
# --------------------------------------------------------------------------

DENIED_WRITES = [
    (ServiceName.DISPATCHER, "messages"),
    (ServiceName.API, "outbound_messages"),
    (ServiceName.OUTBOX_PUBLISHER, "users"),
    (ServiceName.MESSAGE_AGGREGATOR, "outbound_messages"),
    (ServiceName.WORKFLOW_WORKER, "messages"),
]


@pytest.mark.parametrize(("service", "table"), DENIED_WRITES)
async def test_service_role_is_refused_the_writes_it_should_not_have(
    migrated_database: ApplicationSettings, service: ServiceName, table: str
) -> None:
    assert not ({"INSERT", "UPDATE"} & set(SERVICE_GRANTS[service].get(table, ()))), (
        "the policy under test must deny this write"
    )

    engine = create_async_engine(migrated_database.postgres.dsn_for(service))
    try:
        async with engine.connect() as connection:
            with pytest.raises(ProgrammingError) as excinfo:
                await connection.execute(sa.text(f"UPDATE {table} SET updated_at = now()"))
    finally:
        await engine.dispose()

    assert "permission denied" in str(excinfo.value).lower()


@pytest.mark.parametrize("service", list(ServiceName))
async def test_service_role_can_perform_the_writes_it_needs(
    migrated_database: ApplicationSettings, service: ServiceName
) -> None:
    """Least privilege that blocks legitimate work is an outage, not a control."""
    writable = [
        table
        for table, privileges in SERVICE_GRANTS[service].items()
        if "UPDATE" in privileges or "INSERT" in privileges
    ]
    assert writable, f"{service.value} has no write grants at all"

    engine = create_async_engine(migrated_database.postgres.dsn_for(service))
    try:
        async with engine.connect() as connection:
            for table in writable:
                # An UPDATE over an empty table touches no rows but still needs
                # the privilege, which is exactly what is under test.
                if "UPDATE" in SERVICE_GRANTS[service][table]:
                    await connection.execute(sa.text(f"UPDATE {table} SET updated_at = now()"))
                await connection.execute(sa.text(f"SELECT * FROM {table} LIMIT 1"))
    finally:
        await engine.dispose()


async def test_domain_events_are_append_only_for_every_service(
    migrated_database: ApplicationSettings,
) -> None:
    for service in ServiceName:
        engine = create_async_engine(migrated_database.postgres.dsn_for(service))
        try:
            async with engine.connect() as connection:
                with pytest.raises(ProgrammingError):
                    await connection.execute(sa.text("UPDATE domain_events SET event_version = 2"))
        finally:
            await engine.dispose()
