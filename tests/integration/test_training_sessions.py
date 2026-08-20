"""WS-5 against real PostgreSQL: the lifecycle of a training session (§18, Q31).

The assertions that matter are about *who* and *when*: a session closed with
nobody attributed is the gap the audit trail exists to prevent, and a session
closed on a Redis hint rather than on `last_activity_at` is §18 quietly
stopping being true.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.services.training_sessions import TrainingSessionManager
from app.config import ApplicationSettings
from app.domain.training.sessions import SessionCloseReason
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    ActorType,
    AuditEvent,
    Conversation,
    DomainEvent,
    OutboxEvent,
    TrainingSession,
    TrainingSessionStatus,
    User,
)
from app.workers.session_expiration_worker import SessionExpirationWorker

pytestmark = [pytest.mark.integration]

TIMEOUT = timedelta(hours=3)


@pytest.fixture
def manager() -> TrainingSessionManager:
    return TrainingSessionManager(timeout=TIMEOUT)


@pytest.fixture
async def user_and_conversation(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID]:
    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        return user.id, conversation.id


async def _age_the_session(
    session_factory: async_sessionmaker[AsyncSession], *, by: timedelta
) -> UUID:
    """Push the open session's last activity into the past."""
    async with unit_of_work(session_factory) as session:
        training_session = (
            await session.scalars(
                sa.select(TrainingSession).where(
                    TrainingSession.status == TrainingSessionStatus.ACTIVE
                )
            )
        ).one()
        training_session.last_activity_at = datetime.now(UTC) - by
        return training_session.id


async def _audit_rows(
    session_factory: async_sessionmaker[AsyncSession], action: str
) -> list[AuditEvent]:
    async with session_factory() as session:
        rows = await session.scalars(sa.select(AuditEvent).where(AuditEvent.action == action))
        return list(rows.all())


# --------------------------------------------------------------------------
# Opening and resuming
# --------------------------------------------------------------------------


async def test_the_first_log_opens_a_session(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        training_session, opened = await manager.start_or_resume(
            session, user_id=user_id, conversation_id=conversation_id
        )

    assert opened is True
    assert training_session.status is TrainingSessionStatus.ACTIVE


async def test_a_second_log_inside_the_timeout_resumes_the_same_session(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        first, _ = await manager.start_or_resume(
            session, user_id=user_id, conversation_id=conversation_id
        )
        first_id, first_activity = first.id, first.last_activity_at

    async with unit_of_work(session_factory) as session:
        second, opened = await manager.start_or_resume(
            session, user_id=user_id, conversation_id=conversation_id
        )
        await manager.touch(session, second)
        second_id = second.id

    async with session_factory() as session:
        refreshed = await session.get(TrainingSession, first_id)

    assert opened is False
    assert second_id == first_id
    assert refreshed is not None
    assert refreshed.last_activity_at > first_activity, "touch must move the authority forward"


async def test_a_log_after_the_timeout_closes_the_old_session_and_opens_a_new_one(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    """Q31's lazy fallback: the next workout input is what notices."""
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)
    stale_id = await _age_the_session(session_factory, by=TIMEOUT + timedelta(minutes=1))

    async with unit_of_work(session_factory) as session:
        fresh, opened = await manager.start_or_resume(
            session, user_id=user_id, conversation_id=conversation_id
        )
        fresh_id = fresh.id

    async with session_factory() as session:
        closed = await session.get(TrainingSession, stale_id)

    assert opened is True
    assert fresh_id != stale_id
    assert closed is not None
    assert closed.status is TrainingSessionStatus.CLOSED
    assert closed.closed_by == SessionCloseReason.LAZY.value
    assert closed.finished_at is not None


async def test_two_concurrent_logs_open_exactly_one_session(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    """The partial unique index decides, not the service. Asserted on the row
    count rather than on the return values, because two callers can both
    believe they won."""
    user_id, conversation_id = user_and_conversation

    async def open_one() -> None:
        async with unit_of_work(session_factory) as session:
            await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)

    await asyncio.gather(open_one(), open_one(), open_one())

    async with session_factory() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(TrainingSession))

    assert count == 1


# --------------------------------------------------------------------------
# The background sweep (Q31)
# --------------------------------------------------------------------------


async def test_the_worker_closes_a_stale_session(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)
    stale_id = await _age_the_session(session_factory, by=TIMEOUT + timedelta(minutes=5))

    worker = SessionExpirationWorker(session_factory=session_factory, manager=manager)
    closed_ids = await worker.run_once()

    async with session_factory() as session:
        closed = await session.get(TrainingSession, stale_id)

    assert closed_ids == [stale_id]
    assert closed is not None
    assert closed.status is TrainingSessionStatus.CLOSED
    assert closed.closed_by == SessionCloseReason.WORKER.value


async def test_the_worker_leaves_a_live_session_alone(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)

    worker = SessionExpirationWorker(session_factory=session_factory, manager=manager)

    assert await worker.run_once() == []


async def test_a_stale_hint_cannot_close_a_live_session(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    """§18: Redis may say "look here"; only `last_activity_at` says "act". A
    hint that outlived the activity it described must close nothing."""
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)

    async with unit_of_work(session_factory) as session:
        closed = await manager.close_expired(session, candidates=[user_id])

    assert closed == []


# --------------------------------------------------------------------------
# Attribution (§15, §26)
# --------------------------------------------------------------------------


async def test_opening_a_session_is_audited(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    """A lifecycle attributable only at its end is half a trail."""
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)

    rows = await _audit_rows(session_factory, "training_session.started")

    assert len(rows) == 1
    assert rows[0].actor_type is ActorType.USER
    assert rows[0].actor_user_id == user_id


async def test_the_lazy_close_is_attributed_to_the_user(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)
    await _age_the_session(session_factory, by=TIMEOUT + timedelta(minutes=1))

    async with unit_of_work(session_factory) as session:
        await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)

    rows = await _audit_rows(session_factory, "training_session.closed")

    assert len(rows) == 1
    assert rows[0].actor_type is ActorType.USER
    assert rows[0].metadata_["closed_by"] == SessionCloseReason.LAZY.value


async def test_the_worker_close_is_attributed_to_the_system(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    """Nobody was there. Recording a user would attribute an action to someone
    who did not take it."""
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)
    await _age_the_session(session_factory, by=TIMEOUT + timedelta(hours=1))

    worker = SessionExpirationWorker(session_factory=session_factory, manager=manager)
    await worker.run_once()

    rows = await _audit_rows(session_factory, "training_session.closed")

    assert len(rows) == 1
    assert rows[0].actor_type is ActorType.SYSTEM
    assert rows[0].metadata_["closed_by"] == SessionCloseReason.WORKER.value


async def test_every_close_emits_its_domain_event_and_outbox_row(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    """DEC-005: the event and the change commit together, or a consumer never
    learns the session ended."""
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)
    await _age_the_session(session_factory, by=TIMEOUT + timedelta(minutes=1))

    worker = SessionExpirationWorker(session_factory=session_factory, manager=manager)
    await worker.run_once()

    async with session_factory() as session:
        events = (
            await session.scalars(
                sa.select(DomainEvent).where(DomainEvent.event_type == "training_session.finished")
            )
        ).all()
        outbox = (await session.scalars(sa.select(OutboxEvent))).all()

    assert len(events) == 1
    assert len(outbox) == 1


async def _open_and_age(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    by: timedelta,
) -> tuple[UUID, UUID]:
    """A fresh user with a session that went quiet `by` ago. Returns (user, session)."""
    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        opened, _ = await manager.start_or_resume(
            session, user_id=user.id, conversation_id=conversation.id
        )
        opened.last_activity_at = datetime.now(UTC) - by
        return user.id, opened.id


class _StubHints:
    """Stands in for Redis: returns what it is told, or raises."""

    def __init__(self, *, candidates: list[UUID] | None = None, error: Exception | None = None):
        self._candidates = candidates or []
        self._error = error

    async def expiry_candidates(self, *, limit: int = 100) -> list[UUID]:
        if self._error is not None:
            raise self._error
        return self._candidates


async def test_a_session_with_no_hint_is_still_closed(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The hint narrows *where the sweep looks first*, never which sessions
    exist. A hint that expired, or a keyspace holding only some users, must not
    turn into a session that stays open forever (§18)."""
    hinted_user, hinted_session = await _open_and_age(
        manager, session_factory, by=TIMEOUT + timedelta(minutes=1)
    )
    _, unhinted_session = await _open_and_age(
        manager, session_factory, by=TIMEOUT + timedelta(hours=4)
    )

    worker = SessionExpirationWorker(
        session_factory=session_factory,
        manager=manager,
        hints=_StubHints(candidates=[hinted_user]),  # type: ignore[arg-type]
    )
    closed = await worker.run_once()

    assert set(closed) == {hinted_session, unhinted_session}, (
        "a user missing from the hints must still be swept"
    )


async def test_a_redis_outage_does_not_stop_the_sweep(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Redis is non-authoritative (§10, §18), so losing it costs a slower sweep
    and nothing else. A worker that dies on it stops closing sessions entirely
    while the database still knows which ones ended."""
    _, stale_session = await _open_and_age(
        manager, session_factory, by=TIMEOUT + timedelta(minutes=1)
    )

    worker = SessionExpirationWorker(
        session_factory=session_factory,
        manager=manager,
        hints=_StubHints(error=ConnectionError("redis is down")),  # type: ignore[arg-type]
    )
    closed = await worker.run_once()

    assert closed == [stale_session]


async def test_the_expiration_role_cannot_open_a_session(
    migrated_database: ApplicationSettings,
) -> None:
    """A bug that made the sweep *open* a session is refused by PostgreSQL
    rather than discovered later in a user's history."""
    from sqlalchemy.exc import ProgrammingError
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import ServiceName

    engine = create_async_engine(
        migrated_database.postgres.dsn_for(ServiceName.SESSION_EXPIRATION_WORKER)
    )
    try:
        async with engine.connect() as connection:
            with pytest.raises(ProgrammingError):
                await connection.execute(
                    sa.text(
                        "INSERT INTO training_sessions "
                        "(id, user_id, status, created_at, updated_at, started_at, "
                        " last_activity_at, expected_version) "
                        "VALUES (gen_random_uuid(), gen_random_uuid(), 'active', now(), "
                        " now(), now(), now(), 1)"
                    )
                )
    finally:
        await engine.dispose()


async def test_no_role_can_rewrite_the_audit_trail(
    migrated_database: ApplicationSettings,
) -> None:
    """§26 makes it append-only, and an audit trail that can be edited is not
    one. Asserted with each role's own credentials, not the admin's."""
    from sqlalchemy.exc import ProgrammingError
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import ServiceName

    statements = ("UPDATE audit_events SET action = 'tampered'", "DELETE FROM audit_events")

    for service in ServiceName:
        engine = create_async_engine(migrated_database.postgres.dsn_for(service))
        try:
            for statement in statements:
                # A fresh connection per statement: the first refusal aborts
                # the transaction, and everything after it would fail for that
                # reason instead of for the permission being tested.
                async with engine.connect() as connection:
                    with pytest.raises(ProgrammingError):
                        await connection.execute(sa.text(statement))
        finally:
            await engine.dispose()


async def test_metadata_survives_the_round_trip(
    manager: TrainingSessionManager,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_conversation: tuple[UUID, UUID],
) -> None:
    """`metadata` is a reserved attribute name on a SQLAlchemy model, so the
    column is mapped under another one. This catches the mapping breaking."""
    user_id, conversation_id = user_and_conversation

    async with unit_of_work(session_factory) as session:
        await manager.start_or_resume(session, user_id=user_id, conversation_id=conversation_id)

    rows = await _audit_rows(session_factory, "training_session.started")
    stored: dict[str, Any] = rows[0].metadata_

    assert isinstance(stored, dict)
