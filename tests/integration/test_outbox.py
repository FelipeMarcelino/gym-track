"""WS-5 against real PostgreSQL: the atomicity and concurrency claims of DEC-005."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports.event_publisher import EventPublishError
from app.domain.events import DomainEventEnvelope
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    Conversation,
    DomainEvent,
    OutboxEvent,
    OutboxStatus,
    User,
)
from app.infrastructure.postgres.outbox import claim_pending_events, record_domain_event
from app.observability import interaction_scope
from app.workers.outbox_publisher import OutboxPublisher

pytestmark = [pytest.mark.integration]

EXCHANGE = "domain.events"
ROUTING_KEY = "message.received"


class RecordingPublisher:
    """A publisher that remembers what it was asked to send."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.published: list[DomainEventEnvelope] = []
        self._fail_with = fail_with

    async def publish(
        self, envelope: DomainEventEnvelope, *, exchange: str, routing_key: str
    ) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.published.append(envelope)


def _envelope(aggregate_id: UUID | None = None, **overrides: object) -> DomainEventEnvelope:
    return DomainEventEnvelope(
        event_type="message.received",
        aggregate_type="message",
        aggregate_id=aggregate_id or UUID(int=7),
        **overrides,  # type: ignore[arg-type]
    )


async def _seed_user(session_factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    async with unit_of_work(session_factory) as session:
        user = User(locale="pt-BR", timezone="UTC")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        return user.id, conversation.id


async def test_event_and_outbox_row_commit_together(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with unit_of_work(session_factory) as session:
        await record_domain_event(session, _envelope(), exchange=EXCHANGE, routing_key=ROUTING_KEY)

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(DomainEvent)) == 1
        row = (await session.scalars(sa.select(OutboxEvent))).one()
        assert row.status is OutboxStatus.PENDING
        assert row.exchange == EXCHANGE
        assert row.routing_key == ROUTING_KEY


async def test_a_rolled_back_transaction_leaves_no_outbox_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The atomicity claim of DEC-005: no event without its change, and no
    change without its event."""

    class DomainRollbackError(RuntimeError):
        pass

    with pytest.raises(DomainRollbackError):
        async with unit_of_work(session_factory) as session:
            user = User(locale="pt-BR", timezone="UTC")
            session.add(user)
            await session.flush()
            await record_domain_event(
                session,
                _envelope(aggregate_id=user.id),
                exchange=EXCHANGE,
                routing_key=ROUTING_KEY,
            )
            raise DomainRollbackError

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(DomainEvent)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(User)) == 0


async def test_the_recorded_event_inherits_the_ambient_correlation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with interaction_scope(["req-1"]) as context:
        async with unit_of_work(session_factory) as session:
            await record_domain_event(
                session, _envelope(), exchange=EXCHANGE, routing_key=ROUTING_KEY
            )

    async with session_factory() as session:
        event = (await session.scalars(sa.select(DomainEvent))).one()

    assert event.trace_id == context.trace_id
    assert event.correlation_id == context.correlation_id


async def test_two_concurrent_publishers_never_claim_the_same_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FOR UPDATE SKIP LOCKED is what makes a second publisher free rather than
    dangerous."""
    async with unit_of_work(session_factory) as session:
        for index in range(20):
            await record_domain_event(
                session,
                _envelope(aggregate_id=UUID(int=index)),
                exchange=EXCHANGE,
                routing_key=ROUTING_KEY,
            )

    first = RecordingPublisher()
    second = RecordingPublisher()
    results = await asyncio.gather(
        OutboxPublisher(session_factory, first, batch_size=20).publish_pending(),
        OutboxPublisher(session_factory, second, batch_size=20).publish_pending(),
    )

    published = [envelope.event_id for envelope in first.published + second.published]
    assert len(set(published)) == len(published), "a row was published by both publishers"
    assert sum(result.published for result in results) == 20

    async with session_factory() as session:
        pending = await session.scalar(
            sa.select(sa.func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.PENDING)
        )
    assert pending == 0


async def test_a_publish_failure_leaves_the_row_pending_for_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with unit_of_work(session_factory) as session:
        await record_domain_event(session, _envelope(), exchange=EXCHANGE, routing_key=ROUTING_KEY)

    publisher = OutboxPublisher(
        session_factory, RecordingPublisher(fail_with=EventPublishError("broker down"))
    )
    result = await publisher.publish_pending()

    assert result == result.__class__(published=0, failed=1)

    async with session_factory() as session:
        row = (await session.scalars(sa.select(OutboxEvent))).one()

    assert row.status is OutboxStatus.PENDING, "a failed publish must not lose the event"
    assert row.attempts == 1
    assert row.last_error is not None and "broker down" in row.last_error
    assert row.available_at > datetime.now(UTC), "the retry is scheduled, not immediate"


async def test_a_row_backing_off_is_not_claimed_before_it_is_due(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with unit_of_work(session_factory) as session:
        await record_domain_event(session, _envelope(), exchange=EXCHANGE, routing_key=ROUTING_KEY)

    failing = OutboxPublisher(
        session_factory, RecordingPublisher(fail_with=EventPublishError("broker down"))
    )
    await failing.publish_pending()

    working = RecordingPublisher()
    assert (await OutboxPublisher(session_factory, working).publish_pending()).claimed == 0
    assert working.published == []

    async with session_factory() as session:
        rows = await claim_pending_events(
            session, limit=10, now=datetime.now(UTC) + timedelta(minutes=10)
        )
    assert len(rows) == 1, "once the backoff elapses the row is claimable again"


async def test_publication_is_idempotent_for_consumers_through_event_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§27 tolerates duplicate publication; `event_id` is what makes that safe."""
    async with unit_of_work(session_factory) as session:
        envelope = await record_domain_event(
            session, _envelope(), exchange=EXCHANGE, routing_key=ROUTING_KEY
        )

    publisher = RecordingPublisher()
    await OutboxPublisher(session_factory, publisher).publish_pending()

    # A crash between publish and commit would replay the same row.
    async with unit_of_work(session_factory) as session:
        row = (await session.scalars(sa.select(OutboxEvent))).one()
        row.status = OutboxStatus.PENDING
    await OutboxPublisher(session_factory, publisher).publish_pending()

    assert [event.event_id for event in publisher.published] == [
        envelope.event_id,
        envelope.event_id,
    ]
    assert len({event.event_id for event in publisher.published}) == 1


async def test_the_published_payload_is_the_frozen_wire_format(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, conversation_id = await _seed_user(session_factory)

    async with unit_of_work(session_factory) as session:
        recorded = await record_domain_event(
            session,
            _envelope(
                aggregate_id=conversation_id,
                user_id=user_id,
                payload={"conversation_id": str(conversation_id)},
            ),
            exchange=EXCHANGE,
            routing_key=ROUTING_KEY,
        )

    publisher = RecordingPublisher()
    await OutboxPublisher(session_factory, publisher).publish_pending()

    assert publisher.published[0] == recorded


async def test_a_poison_row_does_not_stall_the_rest_of_the_batch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A payload the current schema rejects — corruption, or a rolling deploy
    mid-version-change — must fail alone, not take every pass down with it."""
    async with unit_of_work(session_factory) as session:
        poison = await record_domain_event(
            session, _envelope(aggregate_id=UUID(int=1)), exchange=EXCHANGE, routing_key=ROUTING_KEY
        )
        healthy = await record_domain_event(
            session, _envelope(aggregate_id=UUID(int=2)), exchange=EXCHANGE, routing_key=ROUTING_KEY
        )

    async with unit_of_work(session_factory) as session:
        row = (
            await session.scalars(
                sa.select(OutboxEvent).where(OutboxEvent.domain_event_id == poison.event_id)
            )
        ).one()
        row.payload = {"event_type": "message.received", "unknown_field": True}

    publisher = RecordingPublisher()
    result = await OutboxPublisher(session_factory, publisher).publish_pending()

    assert result.published == 1
    assert result.failed == 1
    assert [event.event_id for event in publisher.published] == [healthy.event_id]

    async with session_factory() as session:
        rows = {
            row.domain_event_id: row
            for row in (await session.scalars(sa.select(OutboxEvent))).all()
        }

    assert rows[healthy.event_id].status is OutboxStatus.PUBLISHED
    assert rows[poison.event_id].status is OutboxStatus.PENDING
    assert rows[poison.event_id].attempts == 1
    assert rows[poison.event_id].last_error is not None, "a stuck row must be visibly stuck"


async def test_a_partial_correlation_is_completed_rather_than_dropped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An envelope carrying a trace but no correlation would otherwise persist a
    null correlation, and the consumer would mint a fresh one — severing the
    event from the interaction that caused it."""
    with interaction_scope(["req-1"]) as context:
        async with unit_of_work(session_factory) as session:
            await record_domain_event(
                session,
                _envelope(trace_id="explicit-trace"),
                exchange=EXCHANGE,
                routing_key=ROUTING_KEY,
            )

    async with session_factory() as session:
        event = (await session.scalars(sa.select(DomainEvent))).one()

    assert event.trace_id == "explicit-trace", "an explicit value is never overwritten"
    assert event.correlation_id == context.correlation_id


async def test_both_rows_persist_the_same_payload_even_if_the_caller_mutates_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The envelope is frozen, but its payload dict is not: sharing it between
    the two rows would let one mutation persist two payloads under one id."""
    payload = {"conversation_id": "c-1"}

    async with unit_of_work(session_factory) as session:
        recorded = await record_domain_event(
            session, _envelope(payload=payload), exchange=EXCHANGE, routing_key=ROUTING_KEY
        )
        payload["conversation_id"] = "mutated-after-the-fact"

    async with session_factory() as session:
        event = (await session.scalars(sa.select(DomainEvent))).one()
        outbox = (await session.scalars(sa.select(OutboxEvent))).one()

    assert event.payload == outbox.payload["payload"]
    assert event.payload == {"conversation_id": "c-1"}
    assert recorded.event_id == outbox.domain_event_id
