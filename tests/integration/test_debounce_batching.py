"""WS-8 with real Redis and PostgreSQL: batching, staleness and recovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ApplicationSettings
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    Conversation,
    DomainEvent,
    Message,
    MessageBatch,
    MessageBatchItem,
    MessageContentType,
    MessageDirection,
    MessagingProvider,
    OutboxEvent,
    User,
)
from app.infrastructure.rabbitmq.partitioning import queue_for_user
from app.infrastructure.redis.debounce import RedisDebounceStore
from app.workers.message_aggregator import (
    INPUT_BATCH_READY,
    FlushScheduler,
    FlushTrigger,
    MessageAggregator,
)

pytestmark = [pytest.mark.integration]


class RecordingScheduler(FlushScheduler):
    """Captures triggers instead of publishing them, so a test controls time."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[dict[str, Any], int]] = []
        super().__init__(self._record)

    async def _record(self, payload: dict[str, Any], *, delay_ms: int) -> None:
        self.scheduled.append((payload, delay_ms))

    @property
    def last(self) -> dict[str, Any]:
        return self.scheduled[-1][0]


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    await client.flushall()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def store(redis: Redis, migrated_database: ApplicationSettings) -> RedisDebounceStore:
    return RedisDebounceStore(redis, absolute_window=migrated_database.workflow.max_batch_window)


@pytest.fixture
def scheduler() -> RecordingScheduler:
    return RecordingScheduler()


@pytest.fixture
def aggregator(
    session_factory: async_sessionmaker[AsyncSession],
    store: RedisDebounceStore,
    scheduler: RecordingScheduler,
    migrated_database: ApplicationSettings,
) -> MessageAggregator:
    return MessageAggregator(
        session_factory=session_factory,
        store=store,
        scheduler=scheduler,
        settings=migrated_database,
    )


@pytest.fixture
async def conversation(
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


async def _add_message(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    conversation_id: UUID,
    text: str,
    received_at: datetime | None = None,
) -> UUID:
    async with unit_of_work(session_factory) as session:
        message = Message(
            user_id=user_id,
            conversation_id=conversation_id,
            provider=MessagingProvider.WHATSAPP,
            external_message_id=f"wamid.{uuid4()}",
            direction=MessageDirection.INBOUND,
            content_type=MessageContentType.TEXT,
            text=text,
            received_at=received_at or datetime.now(UTC),
            trace_id=uuid4().hex,
        )
        session.add(message)
        await session.flush()
        return message.id


async def _register(
    aggregator: MessageAggregator,
    *,
    user_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
) -> None:
    await aggregator.on_message_received(
        {
            "user_id": str(user_id),
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
        }
    )


async def _count(session_factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with session_factory() as session:
        return int(await session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


async def test_three_fragments_inside_the_window_become_one_batch(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
) -> None:
    """The demo of the sprint: three messages, one batch, one workflow input."""
    user_id, conversation_id = conversation
    for index, text in enumerate(["fiz supino", "3x10", "80kg"]):
        message_id = await _add_message(
            session_factory,
            user_id=user_id,
            conversation_id=conversation_id,
            text=text,
            received_at=datetime.now(UTC) + timedelta(milliseconds=index),
        )
        await _register(
            aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )

    # Only the last trigger is current; the first two are stale by construction.
    assert await aggregator.on_flush(scheduler.scheduled[0][0]) is None
    assert await aggregator.on_flush(scheduler.scheduled[1][0]) is None
    batch_id = await aggregator.on_flush(scheduler.last)

    assert batch_id is not None

    async with session_factory() as session:
        batches = (await session.scalars(sa.select(MessageBatch))).all()
        items = (
            await session.scalars(sa.select(MessageBatchItem).order_by(MessageBatchItem.position))
        ).all()

    assert len(batches) == 1
    assert [item.position for item in items] == [0, 1, 2]


async def test_batch_items_preserve_arrival_order(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
) -> None:
    """Order comes from the durable rows, not from Redis (§10)."""
    user_id, conversation_id = conversation
    base = datetime.now(UTC) - timedelta(seconds=5)
    expected = []
    for index, text in enumerate(["primeiro", "segundo", "terceiro"]):
        message_id = await _add_message(
            session_factory,
            user_id=user_id,
            conversation_id=conversation_id,
            text=text,
            received_at=base + timedelta(seconds=index),
        )
        expected.append(message_id)
        await _register(
            aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )

    await aggregator.on_flush(scheduler.last)

    async with session_factory() as session:
        items = (
            await session.scalars(sa.select(MessageBatchItem).order_by(MessageBatchItem.position))
        ).all()

    assert [item.message_id for item in items] == expected


async def test_a_stale_trigger_is_dropped_without_emitting(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
) -> None:
    user_id, conversation_id = conversation
    for text in ("um", "dois"):
        message_id = await _add_message(
            session_factory, user_id=user_id, conversation_id=conversation_id, text=text
        )
        await _register(
            aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )

    assert await aggregator.on_flush(scheduler.scheduled[0][0]) is None

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(MessageBatch)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(DomainEvent)) == 0


async def test_a_flush_emits_input_batch_ready_to_the_users_partition(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
    migrated_database: ApplicationSettings,
) -> None:
    user_id, conversation_id = conversation
    message_id = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="oi"
    )
    await _register(
        aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
    )

    batch_id = await aggregator.on_flush(scheduler.last)

    async with session_factory() as session:
        event = (await session.scalars(sa.select(DomainEvent))).one()
        outbox = (await session.scalars(sa.select(OutboxEvent))).one()

    assert event.event_type == INPUT_BATCH_READY
    assert event.aggregate_id == batch_id
    assert outbox.routing_key == queue_for_user(user_id, migrated_database.workflow.partitions)
    assert outbox.exchange == "workflow"


async def test_the_batch_carries_one_interaction_trace_linking_the_requests(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
) -> None:
    """Q131: the interaction trace is minted here, at batch persistence."""
    user_id, conversation_id = conversation
    request_traces = []
    for text in ("a", "b", "c"):
        message_id = await _add_message(
            session_factory, user_id=user_id, conversation_id=conversation_id, text=text
        )
        await _register(
            aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        async with session_factory() as session:
            message = await session.get(Message, message_id)
            assert message is not None
            request_traces.append(message.trace_id)

    await aggregator.on_flush(scheduler.last)

    async with session_factory() as session:
        batch = (await session.scalars(sa.select(MessageBatch))).one()
        event = (await session.scalars(sa.select(DomainEvent))).one()

    assert batch.trace_id is not None
    assert batch.trace_id not in request_traces, "the batch trace is its own trace"
    assert event.trace_id == batch.trace_id
    assert len(set(request_traces)) == 3


async def test_every_debounce_key_has_a_ttl(
    aggregator: MessageAggregator,
    store: RedisDebounceStore,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
) -> None:
    """A key that outlives its window is a leak that surfaces weeks later."""
    user_id, conversation_id = conversation
    message_id = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="oi"
    )
    await _register(
        aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
    )

    ttl = await store.ttl_of(user_id=user_id, conversation_id=conversation_id)

    assert ttl > 0, "-1 means no expiry, -2 means no key"


async def test_the_second_flush_of_one_window_produces_nothing(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
) -> None:
    """Clearing the window on read is what stops one window becoming two
    batches — and two workflow executions for one interaction."""
    user_id, conversation_id = conversation
    message_id = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="oi"
    )
    await _register(
        aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
    )

    assert await aggregator.on_flush(scheduler.last) is not None
    assert await aggregator.on_flush(scheduler.last) is None

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(MessageBatch)) == 1


async def test_redis_flushed_mid_window_loses_no_message(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
) -> None:
    """§10 declares Redis non-authoritative, so this must cost a late batch and
    never a lost message: the fragments are already durable in `messages`."""
    user_id, conversation_id = conversation
    first = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="perdido?"
    )
    await _register(aggregator, user_id=user_id, conversation_id=conversation_id, message_id=first)

    await redis.flushall()

    second = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="segundo"
    )
    await _register(aggregator, user_id=user_id, conversation_id=conversation_id, message_id=second)
    await aggregator.on_flush(scheduler.last)

    async with session_factory() as session:
        items = (
            await session.scalars(sa.select(MessageBatchItem).order_by(MessageBatchItem.position))
        ).all()

    assert [item.message_id for item in items] == [first, second], (
        "the fragment whose debounce state was wiped must still reach a batch"
    )


async def test_a_trigger_for_an_unknown_window_does_nothing(
    aggregator: MessageAggregator,
    conversation: tuple[UUID, UUID],
) -> None:
    user_id, conversation_id = conversation
    trigger = FlushTrigger(user_id=user_id, conversation_id=conversation_id, generation=1)

    assert await aggregator.on_flush(trigger.to_payload()) is None


async def test_the_scheduled_delay_shrinks_as_the_cap_approaches(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    store: RedisDebounceStore,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
    migrated_database: ApplicationSettings,
) -> None:
    """A message at 9s into a 10s cap must not push the flush past the cap."""
    user_id, conversation_id = conversation
    message_id = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="oi"
    )
    await _register(
        aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
    )

    assert scheduler.scheduled[0][1] == int(
        migrated_database.workflow.debounce_window.total_seconds() * 1000
    )

    # Rewind the window's start so the cap is one second away.
    from app.domain.debounce import key_for

    started = datetime.now(UTC) - migrated_database.workflow.max_batch_window + timedelta(seconds=1)
    await redis.hset(
        key_for(str(user_id), str(conversation_id)),
        "window_started_at",
        started.isoformat(),
    )

    later = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="ainda"
    )
    await _register(aggregator, user_id=user_id, conversation_id=conversation_id, message_id=later)

    assert scheduler.scheduled[-1][1] <= 1000, "the delay was capped by the absolute window"


async def test_a_trigger_without_a_window_still_batches_what_is_waiting(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
) -> None:
    """A durable trigger outliving its Redis key must not become a no-op: the
    fragments are in `messages`, and Redis is not the authority (§10)."""
    user_id, conversation_id = conversation
    message_id = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="oi"
    )
    await _register(
        aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
    )

    await redis.flushall()

    assert await aggregator.on_flush(scheduler.last) is not None

    async with session_factory() as session:
        item = (await session.scalars(sa.select(MessageBatchItem))).one()

    assert item.message_id == message_id


async def test_a_redelivered_registration_does_not_produce_a_second_batch(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
) -> None:
    """RabbitMQ delivery is at-least-once, so the same event arrives twice. The
    message is already in a batch, so the second window has nothing to take."""
    user_id, conversation_id = conversation
    message_id = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="oi"
    )
    await _register(
        aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
    )
    assert await aggregator.on_flush(scheduler.last) is not None

    await _register(
        aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
    )
    assert await aggregator.on_flush(scheduler.last) is None

    assert await _count(session_factory, MessageBatch) == 1
    assert await _count(session_factory, MessageBatchItem) == 1


async def test_a_failed_persist_leaves_the_window_claimable(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    store: RedisDebounceStore,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the window before the commit would strand every fragment in it
    when the database is briefly unavailable."""
    user_id, conversation_id = conversation
    message_id = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="oi"
    )
    await _register(
        aggregator, user_id=user_id, conversation_id=conversation_id, message_id=message_id
    )

    class TransientOutageError(RuntimeError):
        pass

    async def failing_persist(*args: Any, **kwargs: Any) -> None:
        raise TransientOutageError

    monkeypatch.setattr(aggregator, "_persist_batch", failing_persist)
    with pytest.raises(TransientOutageError):
        await aggregator.on_flush(scheduler.last)

    monkeypatch.undo()
    assert await store.read(user_id=user_id, conversation_id=conversation_id) is not None
    assert await aggregator.on_flush(scheduler.last) is not None


async def test_a_fragment_arriving_during_a_flush_keeps_its_own_window(
    aggregator: MessageAggregator,
    scheduler: RecordingScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    conversation: tuple[UUID, UUID],
) -> None:
    """It must not be swept into a batch whose flush was already decided —
    otherwise it loses the debounce interval it was entitled to."""
    user_id, conversation_id = conversation
    first = await _add_message(
        session_factory, user_id=user_id, conversation_id=conversation_id, text="primeiro"
    )
    await _register(aggregator, user_id=user_id, conversation_id=conversation_id, message_id=first)

    # Arrives "later" than the flush's claim time.
    late = await _add_message(
        session_factory,
        user_id=user_id,
        conversation_id=conversation_id,
        text="tarde",
        received_at=datetime.now(UTC) + timedelta(seconds=5),
    )

    await aggregator.on_flush(scheduler.last)

    async with session_factory() as session:
        items = (await session.scalars(sa.select(MessageBatchItem))).all()

    assert [item.message_id for item in items] == [first]
    assert late not in [item.message_id for item in items]
