"""Correlation across process boundaries (§30.3, Q131, WS-11 item 44).

The claim: three webhook requests produce three request traces, the aggregator
mints exactly one interaction trace when it persists the batch, and that trace
appears in the workflow worker's and the dispatcher's log lines for the same
interaction — with every contributing request trace reachable from it.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.postgres.models import Message, MessageBatch, OutboundMessage
from app.infrastructure.rabbitmq.partitioning import queue_for_user
from app.infrastructure.rabbitmq.topology import DEBOUNCE_FLUSH_QUEUE
from app.observability import configure_logging
from tests.e2e.conftest import Skeleton

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    try:
        yield stream
    finally:
        logging.getLogger().handlers.clear()


def _records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


async def test_one_interaction_trace_reaches_every_stage(
    skeleton: Skeleton,
    session_factory: async_sessionmaker[AsyncSession],
    log_stream: io.StringIO,
) -> None:
    for index, text in enumerate(("fiz supino", "3x10", "80kg")):
        await skeleton.send_webhook(f"wamid.{index}", text)

    await skeleton.publish_outbox()
    await skeleton.drain("message.received", skeleton.aggregator.on_message_received)
    await asyncio.sleep(skeleton.settings.workflow.debounce_window.total_seconds() + 1)
    await skeleton.drain(DEBOUNCE_FLUSH_QUEUE, skeleton.aggregator.on_flush)

    async with session_factory() as session:
        batch = (await session.scalars(sa.select(MessageBatch))).one()
        request_traces = {
            message.trace_id for message in (await session.scalars(sa.select(Message))).all()
        }

    assert len(request_traces) == 3, "three webhook requests, three request traces"
    assert batch.trace_id not in request_traces, "the interaction trace is its own"

    await skeleton.publish_outbox()
    await skeleton.drain(queue_for_user(batch.user_id, skeleton.partitions), skeleton.worker.handle)
    await skeleton.publish_outbox()
    await skeleton.drain("outbound.dispatch", skeleton.dispatcher.dispatch)

    traced = {
        record["logger"]
        for record in _records(log_stream)
        if record.get("trace_id") == batch.trace_id
    }

    assert any("workflow_worker" in logger for logger in traced), (
        "the worker logged under the interaction trace"
    )
    assert any("dispatcher" in logger for logger in traced), (
        "the dispatcher logged under the same trace"
    )

    async with session_factory() as session:
        outbound = (await session.scalars(sa.select(OutboundMessage))).one()

    assert outbound.trace_id == batch.trace_id


async def test_each_contributing_request_trace_is_reachable_from_the_batch(
    skeleton: Skeleton, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Q131's linkage, stored rather than only logged: the batch's items lead
    back to the messages, and each message carries the request trace that
    persisted it."""
    for index in range(3):
        await skeleton.send_webhook(f"wamid.{index}", f"fragmento {index}")

    await skeleton.publish_outbox()
    await skeleton.drain("message.received", skeleton.aggregator.on_message_received)
    await asyncio.sleep(skeleton.settings.workflow.debounce_window.total_seconds() + 1)
    await skeleton.drain(DEBOUNCE_FLUSH_QUEUE, skeleton.aggregator.on_flush)

    async with session_factory() as session:
        batch = (await session.scalars(sa.select(MessageBatch))).one()
        linked = (
            (
                await session.execute(
                    sa.text(
                        "SELECT m.trace_id FROM messages m "
                        "JOIN message_batch_items i ON i.message_id = m.id "
                        "WHERE i.message_batch_id = :batch_id ORDER BY i.position"
                    ),
                    {"batch_id": batch.id},
                )
            )
            .scalars()
            .all()
        )

    assert len(linked) == 3
    assert len(set(linked)) == 3, "each fragment kept its own request trace"
    assert batch.trace_id not in linked


async def test_a_broker_outage_during_publication_loses_no_event(
    skeleton: Skeleton, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """§38's broker-disconnect injection: the outbox row stays PENDING and is
    published once the broker is reachable again."""
    from app.infrastructure.postgres.models import OutboxEvent, OutboxStatus
    from app.workers.outbox_publisher import OutboxPublisher

    await skeleton.send_webhook("wamid.0", "oi")

    class DisconnectedPublisher:
        async def publish(self, envelope: Any, *, exchange: str, routing_key: str) -> None:
            raise ConnectionError("broker unreachable")

    result = await OutboxPublisher(session_factory, DisconnectedPublisher()).publish_pending()

    assert result.failed == 1

    async with session_factory() as session:
        row = (await session.scalars(sa.select(OutboxEvent))).one()

    assert row.status is OutboxStatus.PENDING, "nothing was lost while the broker was down"
    assert row.attempts == 1

    # The row is due again after its backoff; publishing then succeeds.
    async with session_factory() as session:
        pending = (await session.scalars(sa.select(OutboxEvent))).one()
        pending.available_at = pending.created_at
        await session.commit()

    assert (await skeleton.publish_outbox()) == 1

    async with session_factory() as session:
        row = (await session.scalars(sa.select(OutboxEvent))).one()

    assert row.status is OutboxStatus.PUBLISHED
