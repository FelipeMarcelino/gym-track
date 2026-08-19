"""The sprint's demo, asserted from the outside (§38, WS-11).

Three fragmented messages go in through the webhook and one ordered reply comes
out of the fake WhatsApp client, having crossed a real broker, a real Redis and
a real PostgreSQL on the way.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.graphs.main.handlers import ACKNOWLEDGEMENT
from app.infrastructure.postgres.models import (
    MessageBatch,
    MessageBatchItem,
    OutboundMessage,
    OutboxEvent,
    OutboxStatus,
    WorkflowExecution,
)
from app.infrastructure.rabbitmq.partitioning import queue_for_user
from app.infrastructure.rabbitmq.topology import DEBOUNCE_FLUSH_QUEUE
from tests.e2e.conftest import Skeleton

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

FRAGMENTS = ("fiz supino", "3x10", "80kg")


async def _batch_user(session_factory: async_sessionmaker[AsyncSession]) -> UUID:
    async with session_factory() as session:
        user_id = await session.scalar(sa.select(MessageBatch.user_id))
    assert user_id is not None, "the pipeline was expected to have produced a batch"
    return user_id


async def _run_pipeline(
    skeleton: Skeleton, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Drive every stage the way its process would, in order."""
    await skeleton.publish_outbox()
    await skeleton.drain("message.received", skeleton.aggregator.on_message_received)

    # The flush triggers wait out their per-message TTL in the delay queue.
    await asyncio.sleep(skeleton.settings.workflow.debounce_window.total_seconds() + 1)
    await skeleton.drain(DEBOUNCE_FLUSH_QUEUE, skeleton.aggregator.on_flush)

    await skeleton.publish_outbox()
    user_id = await _batch_user(session_factory)
    await skeleton.drain(queue_for_user(user_id, skeleton.partitions), skeleton.worker.handle)

    await skeleton.publish_outbox()
    await skeleton.drain("outbound.dispatch", skeleton.dispatcher.dispatch)


async def test_three_fragments_produce_one_ordered_reply(
    skeleton: Skeleton, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    for index, text in enumerate(FRAGMENTS):
        assert (await skeleton.send_webhook(f"wamid.{index}", text)).status_code == 202

    await _run_pipeline(skeleton, session_factory)

    assert skeleton.whatsapp.texts == [ACKNOWLEDGEMENT], "one reply, delivered once"

    async with session_factory() as session:
        batches = (await session.scalars(sa.select(MessageBatch))).all()
        items = (
            await session.scalars(sa.select(MessageBatchItem).order_by(MessageBatchItem.position))
        ).all()
        executions = (await session.scalars(sa.select(WorkflowExecution))).all()
        outbound = (await session.scalars(sa.select(OutboundMessage))).all()

    assert len(batches) == 1, "three fragments, one batch"
    assert [item.position for item in items] == [0, 1, 2]
    assert len(executions) == 1
    assert len(outbound) == 1


async def test_every_outbox_row_reaches_published(
    skeleton: Skeleton, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """None are silently lost, which is the operational half of DEC-005."""
    for index, text in enumerate(FRAGMENTS):
        await skeleton.send_webhook(f"wamid.{index}", text)

    await _run_pipeline(skeleton, session_factory)

    async with session_factory() as session:
        rows = (await session.scalars(sa.select(OutboxEvent))).all()

    assert rows, "the pipeline produced events"
    assert all(row.status is OutboxStatus.PUBLISHED for row in rows)
    assert all(row.published_at is not None for row in rows)


async def test_a_redelivered_workflow_message_produces_no_second_reply(
    skeleton: Skeleton, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """§38's failure injection: a worker that commits and dies before acking.

    RabbitMQ redelivers, and the second run must find the work already done.
    """
    await skeleton.send_webhook("wamid.0", "oi")
    await skeleton.publish_outbox()
    await skeleton.drain("message.received", skeleton.aggregator.on_message_received)
    await asyncio.sleep(skeleton.settings.workflow.debounce_window.total_seconds() + 1)
    await skeleton.drain(DEBOUNCE_FLUSH_QUEUE, skeleton.aggregator.on_flush)
    await skeleton.publish_outbox()

    user_id = await _batch_user(session_factory)
    async with session_factory() as session:
        envelope = await session.scalar(
            sa.select(OutboxEvent.payload).where(
                OutboxEvent.routing_key == queue_for_user(user_id, skeleton.partitions)
            )
        )
    assert envelope is not None

    # The message the worker acked, delivered again.
    await skeleton.worker.handle(envelope)
    await skeleton.worker.handle(envelope)

    await skeleton.publish_outbox()
    await skeleton.drain("outbound.dispatch", skeleton.dispatcher.dispatch)

    async with session_factory() as session:
        executions = (await session.scalars(sa.select(WorkflowExecution))).all()
        outbound = (await session.scalars(sa.select(OutboundMessage))).all()

    assert len(executions) == 1, "exactly one workflow execution"
    assert len(outbound) == 1, "exactly one outbound message"
    assert skeleton.whatsapp.texts == [ACKNOWLEDGEMENT]


async def test_a_duplicate_publication_produces_no_duplicate_effect(
    skeleton: Skeleton, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """§27 tolerates duplicate publication; the consumers are what make it safe."""
    await skeleton.send_webhook("wamid.0", "oi")
    await skeleton.publish_outbox()

    async with session_factory() as session:
        envelope = await session.scalar(
            sa.select(OutboxEvent.payload).where(OutboxEvent.routing_key == "message.received")
        )
    assert envelope is not None

    # The same event, consumed three times.
    for _ in range(3):
        await skeleton.aggregator.on_message_received(envelope)

    await asyncio.sleep(skeleton.settings.workflow.debounce_window.total_seconds() + 1)
    await skeleton.drain(DEBOUNCE_FLUSH_QUEUE, skeleton.aggregator.on_flush)

    async with session_factory() as session:
        batches = (await session.scalars(sa.select(MessageBatch))).all()
        items = (await session.scalars(sa.select(MessageBatchItem))).all()

    assert len(batches) == 1
    assert len(items) == 1, "one message belongs to one batch, however often it is announced"


async def test_redis_lost_mid_window_still_answers(
    skeleton: Skeleton,
    session_factory: async_sessionmaker[AsyncSession],
    redis_url: str,
) -> None:
    """§10's claim, end to end: losing the debounce state costs timing, not a
    message."""
    from redis.asyncio import Redis

    await skeleton.send_webhook("wamid.0", "primeiro")
    await skeleton.publish_outbox()
    await skeleton.drain("message.received", skeleton.aggregator.on_message_received)

    wiped = Redis.from_url(redis_url)
    await wiped.flushall()
    await wiped.aclose()

    await skeleton.send_webhook("wamid.1", "segundo")
    await skeleton.publish_outbox()
    await skeleton.drain("message.received", skeleton.aggregator.on_message_received)

    await asyncio.sleep(skeleton.settings.workflow.debounce_window.total_seconds() + 1)
    await skeleton.drain(DEBOUNCE_FLUSH_QUEUE, skeleton.aggregator.on_flush)

    async with session_factory() as session:
        items = (await session.scalars(sa.select(MessageBatchItem))).all()

    assert len(items) == 2, "both fragments reached a batch despite the wipe"
