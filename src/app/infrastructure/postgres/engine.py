"""Async engine, session factory and the transaction boundary (§36, decision D2).

Each process connects with its own database role (Q145), so the engine is built
*for a service*, never from a single shared connection string. Getting that
wrong is how least privilege quietly becomes a comment in a migration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import ApplicationSettings, PostgresRole, PostgresSettings, ServiceName


def build_dsn(postgres: PostgresSettings, role: PostgresRole) -> str:
    password = role.password.get_secret_value()
    return (
        f"postgresql+asyncpg://{role.user}:{password}"
        f"@{postgres.host}:{postgres.port}/{postgres.database}"
    )


def create_engine_for(
    settings: ApplicationSettings,
    service: ServiceName,
    *,
    echo: bool = False,
) -> AsyncEngine:
    """Engine bound to one service's credentials."""
    return create_async_engine(
        settings.postgres.dsn_for(service),
        echo=echo,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def unit_of_work(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction, committed on success and rolled back on any exception.

    DEC-005 rests on this: domain rows, the domain event and the outbox row are
    written by the same unit of work, so there is no window in which an event
    exists without the change it describes, or the reverse.
    """
    async with session_factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()
