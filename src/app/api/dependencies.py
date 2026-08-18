"""What the API needs to do its job, assembled once at startup.

Deliberately small. The API does not hold a broker connection: it enqueues by
writing an outbox row inside the same transaction as the message (§27), and the
outbox publisher is what talks to RabbitMQ. One less thing that can be down
while a webhook is being answered.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import ApplicationSettings


@dataclass(frozen=True, slots=True)
class ApiContext:
    settings: ApplicationSettings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
