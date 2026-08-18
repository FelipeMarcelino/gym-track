"""The outbox publisher process (§27, DEC-005).

One pass: claim a batch of due rows, publish each, mark it. Failures are
per-row, so one broker rejection does not strand the rest of the batch.

Publication is at-least-once by construction. The alternative -- marking the
row PUBLISHED before the broker confirms -- would turn a crash between the two
into a silently lost event, which is precisely what the outbox exists to
prevent. Duplicates are the cheaper failure: consumers deduplicate on
`event_id` (§27, §28).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports.event_publisher import EventPublisher
from app.domain.events import DomainEventEnvelope
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.outbox import claim_pending_events, mark_failed, mark_published
from app.observability.middleware import consumed_message_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublishResult:
    published: int
    failed: int

    @property
    def claimed(self) -> int:
        return self.published + self.failed


class OutboxPublisher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        *,
        batch_size: int = 50,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._batch_size = batch_size

    async def publish_pending(self) -> PublishResult:
        """Publish one batch. Returns what happened, so a caller can pace itself."""
        published = 0
        failed = 0

        async with unit_of_work(self._session_factory) as session:
            rows = await claim_pending_events(session, limit=self._batch_size)

            for row in rows:
                envelope = DomainEventEnvelope.model_validate(row.payload)
                # The event is published under the correlation it was recorded
                # with, so the publish shows up in the interaction's trace
                # rather than in the publisher's own.
                with consumed_message_scope(
                    {"trace_id": envelope.trace_id, "correlation_id": envelope.correlation_id}
                ):
                    try:
                        await self._publisher.publish(
                            envelope,
                            exchange=row.exchange,
                            routing_key=row.routing_key,
                        )
                    except Exception as error:
                        await mark_failed(session, row, repr(error))
                        failed += 1
                        logger.warning(
                            "outbox publication failed",
                            extra={
                                "event_id": str(envelope.event_id),
                                "event_type": envelope.event_type,
                                "attempts": row.attempts,
                            },
                        )
                    else:
                        await mark_published(session, row)
                        published += 1

        return PublishResult(published=published, failed=failed)
