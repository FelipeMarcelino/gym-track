"""The message-aggregator process (§8, §10, Q113, DEC-010, ADR-011).

It does two things, and they are deliberately separate messages rather than a
timer inside the worker:

1. **register** -- a `message.received` event adds the fragment to the debounce
   window and schedules a flush trigger carrying the generation it was
   scheduled for.
2. **flush** -- an expired trigger arrives; if its generation is current (or
   the absolute cap has passed) the window is closed into a `message_batch`
   and `InputBatchReady` is emitted to the user's partition.

An in-process timer would be simpler and would not survive a restart: the
window would sit in Redis with nothing left to fire it. A delayed message
survives, which is why ADR-011 records the mechanism rather than leaving it in
code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ApplicationSettings
from app.domain.debounce import DebounceWindow, next_flush_delay, should_flush
from app.domain.events import DomainEventEnvelope
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    Message,
    MessageBatch,
    MessageBatchItem,
    MessageBatchStatus,
    MessageDirection,
)
from app.infrastructure.postgres.outbox import record_domain_event
from app.infrastructure.rabbitmq.partitioning import queue_for_user
from app.infrastructure.rabbitmq.topology import Exchanges
from app.infrastructure.redis.debounce import RedisDebounceStore
from app.observability import current_context, interaction_scope

logger = logging.getLogger(__name__)

INPUT_BATCH_READY = "workflow.input_batch_ready"


@dataclass(frozen=True, slots=True)
class FlushTrigger:
    """The payload of a delayed flush message."""

    user_id: UUID
    conversation_id: UUID
    generation: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "conversation_id": str(self.conversation_id),
            "generation": self.generation,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> FlushTrigger:
        return cls(
            user_id=UUID(str(payload["user_id"])),
            conversation_id=UUID(str(payload["conversation_id"])),
            generation=int(payload["generation"]),
        )


class FlushScheduler:
    """Publishes a flush trigger to be delivered after a delay."""

    def __init__(self, publish: Any) -> None:
        self._publish = publish

    async def schedule(self, trigger: FlushTrigger, *, delay_ms: int) -> None:
        await self._publish(trigger.to_payload(), delay_ms=delay_ms)


class MessageAggregator:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: RedisDebounceStore,
        scheduler: FlushScheduler,
        settings: ApplicationSettings,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._scheduler = scheduler
        self._settings = settings

    async def on_message_received(self, payload: dict[str, Any]) -> None:
        """Register a fragment and schedule the flush it might trigger."""
        user_id = UUID(str(payload["user_id"]))
        conversation_id = UUID(str(payload["conversation_id"]))

        # Nothing here needs the message id: the fragment is already durable,
        # and the batch is composed from `messages` at flush time. A redelivery
        # of this event therefore costs an extra trigger, never a second batch.
        registered = await self._store.register(user_id=user_id, conversation_id=conversation_id)
        delay = next_flush_delay(
            registered.window,
            now=datetime.now(UTC),
            sliding=self._settings.workflow.debounce_window,
            absolute=self._settings.workflow.max_batch_window,
        )

        await self._scheduler.schedule(
            FlushTrigger(
                user_id=user_id,
                conversation_id=conversation_id,
                generation=registered.window.generation,
            ),
            delay_ms=int(delay.total_seconds() * 1000),
        )

    async def on_flush(self, payload: dict[str, Any]) -> UUID | None:
        """Close the window into a batch, unless this trigger is stale.

        Returns the batch id when one was created, so a caller (and a test) can
        tell a real flush from a dropped trigger.
        """
        trigger = FlushTrigger.from_payload(payload)
        claim_time = datetime.now(UTC)
        window = await self._store.read(
            user_id=trigger.user_id, conversation_id=trigger.conversation_id
        )

        if window is not None and not should_flush(
            scheduled_generation=trigger.generation,
            window=window,
            now=claim_time,
            absolute=self._settings.workflow.max_batch_window,
        ):
            logger.debug(
                "stale flush trigger dropped",
                extra={
                    "scheduled_generation": trigger.generation,
                    "current_generation": window.generation,
                },
            )
            return None

        if window is None:
            # The key expired or Redis lost it. §10 says Redis is not the
            # authority, so a missing window is not "nothing to do" -- the
            # fragments are still in `messages`, waiting to be batched.
            logger.info(
                "flushing without a debounce window; rebuilding from messages",
                extra={"conversation_id": str(trigger.conversation_id)},
            )
            window = DebounceWindow(
                generation=trigger.generation,
                window_started_at=claim_time,
                last_message_at=claim_time,
            )

        batch_id = await self._persist_batch(trigger, window, claim_time)

        # Only now: a failure before the commit leaves the window in place, so
        # the retried trigger still has something to act on.
        await self._store.close(user_id=trigger.user_id, conversation_id=trigger.conversation_id)
        return batch_id

    async def _persist_batch(
        self,
        trigger: FlushTrigger,
        window: DebounceWindow,
        claim_time: datetime,
    ) -> UUID | None:
        async with unit_of_work(self._session_factory) as session:
            # Membership, order and recovery all come from the durable rows.
            ordered = await self._unbatched_messages(session, trigger, claim_time)
            if not ordered:
                return None
            request_traces = [message.trace_id for message in ordered if message.trace_id]

            # The single interaction trace for this batch is minted here, at
            # persistence, linking back to every webhook request that
            # contributed a fragment (Q131).
            with interaction_scope(request_traces) as interaction:
                batch = MessageBatch(
                    user_id=trigger.user_id,
                    conversation_id=trigger.conversation_id,
                    status=MessageBatchStatus.FLUSHED,
                    generation=window.generation,
                    window_started_at=window.window_started_at,
                    flushed_at=datetime.now(UTC),
                    trace_id=interaction.trace_id,
                    correlation_id=interaction.correlation_id,
                )
                session.add(batch)
                await session.flush()

                for position, message in enumerate(ordered):
                    session.add(
                        MessageBatchItem(
                            message_batch_id=batch.id,
                            message_id=message.id,
                            position=position,
                        )
                    )

                await record_domain_event(
                    session,
                    self._input_batch_ready(batch.id, trigger, [message.id for message in ordered]),
                    exchange=Exchanges.WORKFLOW,
                    routing_key=queue_for_user(trigger.user_id, self._settings.workflow.partitions),
                )

                logger.info(
                    "message batch flushed",
                    extra={
                        "message_batch_id": str(batch.id),
                        "messages": len(ordered),
                        "generation": window.generation,
                    },
                )
                return batch.id

    async def _unbatched_messages(
        self, session: AsyncSession, trigger: FlushTrigger, claim_time: datetime
    ) -> list[Message]:
        """Inbound messages of this conversation that no batch has claimed yet.

        This query is what makes Redis genuinely non-authoritative: a fragment
        whose debounce state was lost is still picked up by the next flush of
        its conversation, and a redelivered `message.received` cannot create a
        second batch for a message that already belongs to one.

        The `received_at <= claim_time` bound keeps a fragment that arrives
        while this flush is running out of it, so it still gets its own
        debounce window instead of being swept in early.
        """
        rows = await session.scalars(
            sa.select(Message)
            .outerjoin(MessageBatchItem, MessageBatchItem.message_id == Message.id)
            .where(
                Message.conversation_id == trigger.conversation_id,
                Message.direction == MessageDirection.INBOUND,
                Message.received_at <= claim_time,
                MessageBatchItem.id.is_(None),
            )
            .order_by(Message.received_at, Message.id)
        )
        return list(rows.all())

    def _input_batch_ready(
        self, batch_id: UUID, trigger: FlushTrigger, message_ids: list[UUID]
    ) -> DomainEventEnvelope:
        context = current_context()
        return DomainEventEnvelope(
            event_type=INPUT_BATCH_READY,
            aggregate_type="message_batch",
            aggregate_id=batch_id,
            user_id=trigger.user_id,
            trace_id=context.trace_id if context else None,
            correlation_id=context.correlation_id if context else None,
            payload={
                "message_batch_id": str(batch_id),
                "user_id": str(trigger.user_id),
                "conversation_id": str(trigger.conversation_id),
                "message_ids": [str(message_id) for message_id in message_ids],
            },
        )
