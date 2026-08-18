"""Writing and claiming outbox rows (§27, DEC-005).

The whole point of DEC-005 lives in `record_domain_event`: the event row and
its outbox row are written by the *caller's* session, inside the caller's
transaction. There is therefore no window where a domain change exists without
its event, or an event exists without the change it describes -- the two commit
together or neither does.

Claiming uses `FOR UPDATE SKIP LOCKED` so several publishers can run without
coordinating and without ever handing the same row to two of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.events import DomainEventEnvelope
from app.infrastructure.postgres.models import DomainEvent, OutboxEvent, OutboxStatus
from app.observability import current_context

#: Retry schedule for a failed publication, indexed by attempts already made.
#: Capped rather than unbounded: a row that keeps failing should stay visible
#: and alertable, not drift into a retry an hour away.
BACKOFF_SCHEDULE: Final[tuple[timedelta, ...]] = (
    timedelta(seconds=1),
    timedelta(seconds=5),
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=5),
)


def backoff_for(attempts: int) -> timedelta:
    index = min(max(attempts, 1), len(BACKOFF_SCHEDULE)) - 1
    return BACKOFF_SCHEDULE[index]


async def record_domain_event(
    session: AsyncSession,
    envelope: DomainEventEnvelope,
    *,
    exchange: str,
    routing_key: str,
) -> DomainEventEnvelope:
    """Append the event and its PENDING outbox row in the caller's transaction.

    Correlation is taken from the ambient context when the envelope does not
    carry it, so an event recorded inside a request or a consumed message is
    traceable without every call site restating what the context already knows.
    """
    context = current_context()
    if context is not None:
        # Each field is filled independently: an envelope carrying a trace but
        # no correlation would otherwise be persisted with a null correlation,
        # and the consumer would mint a fresh one -- severing the event from
        # the interaction that caused it.
        envelope = envelope.with_correlation(
            trace_id=envelope.trace_id or context.trace_id,
            correlation_id=envelope.correlation_id or context.correlation_id,
        )

    # One snapshot feeds both rows. Sharing the caller's dict would let a
    # mutation after this call persist two different payloads under one
    # event_id, since the outbox copy is serialized here and the ORM row is
    # serialized at flush time.
    serialized = envelope.model_dump(mode="json")

    session.add(
        DomainEvent(
            id=envelope.event_id,
            event_type=envelope.event_type,
            event_version=envelope.event_version,
            aggregate_type=envelope.aggregate_type,
            aggregate_id=envelope.aggregate_id,
            user_id=envelope.user_id,
            trace_id=envelope.trace_id,
            correlation_id=envelope.correlation_id,
            causation_id=envelope.causation_id,
            payload=serialized["payload"],
            occurred_at=envelope.occurred_at,
        )
    )
    session.add(
        OutboxEvent(
            domain_event_id=envelope.event_id,
            status=OutboxStatus.PENDING,
            exchange=exchange,
            routing_key=routing_key,
            payload=serialized,
        )
    )
    return envelope


async def claim_pending_events(
    session: AsyncSession,
    *,
    limit: int,
    now: datetime | None = None,
) -> list[OutboxEvent]:
    """Take up to `limit` due rows, locked against other publishers.

    `SKIP LOCKED` is what makes horizontal scaling free here: a second
    publisher steps over the rows the first is holding instead of blocking on
    them or, worse, publishing them twice.
    """
    moment = now or datetime.now(UTC)
    statement = (
        sa.select(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.PENDING, OutboxEvent.available_at <= moment)
        .order_by(OutboxEvent.available_at, OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await session.scalars(statement)).all())


async def mark_published(
    session: AsyncSession,
    row: OutboxEvent,
    *,
    now: datetime | None = None,
) -> None:
    row.status = OutboxStatus.PUBLISHED
    row.published_at = now or datetime.now(UTC)
    row.last_error = None


async def mark_failed(
    session: AsyncSession,
    row: OutboxEvent,
    error: str,
    *,
    now: datetime | None = None,
) -> None:
    """Leave the row PENDING, due again after a backoff.

    A failed publication is not a lost event: the row stays claimable, which is
    the property that makes the outbox worth having in the first place.
    """
    moment = now or datetime.now(UTC)
    row.attempts += 1
    row.available_at = moment + backoff_for(row.attempts)
    row.last_error = error[:2000]
