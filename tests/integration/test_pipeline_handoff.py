"""The wiring between stages, using what each stage actually publishes.

Every consumer in this pipeline is fed here from a real `outbox_events` row
rather than from a hand-written dict. That distinction is the whole point of
this file: a fixture that manufactures the shape a consumer happens to expect
will pass while production is broken, which is exactly what happened to the
workflow worker before this test existed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.app import create_app
from app.api.dependencies import ApiContext
from app.config import ApplicationSettings
from app.infrastructure.postgres.models import (
    MessageBatch,
    OutboundMessage,
    OutboxEvent,
    WorkflowExecution,
)
from app.infrastructure.redis.debounce import RedisDebounceStore
from app.security.signatures import SIGNATURE_HEADER, sign
from app.workers.message_aggregator import (
    INPUT_BATCH_READY,
    FlushScheduler,
    MessageAggregator,
)
from app.workers.workflow_worker import WorkflowWorker

pytestmark = [pytest.mark.integration]

BSUID = "5511987654321"


class CapturingScheduler(FlushScheduler):
    def __init__(self) -> None:
        self.scheduled: list[dict[str, Any]] = []
        super().__init__(self._capture)

    async def _capture(self, payload: dict[str, Any], *, delay_ms: int) -> None:
        self.scheduled.append(payload)


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    await client.flushall()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def client(
    migrated_database: ApplicationSettings,
    admin_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        ApiContext(
            settings=migrated_database,
            engine=admin_engine,
            session_factory=session_factory,
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as opened:
        yield opened


async def _send(
    client: httpx.AsyncClient, settings: ApplicationSettings, external_id: str, text: str
) -> None:
    body = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": BSUID,
                                        "id": external_id,
                                        "timestamp": "1755518400",
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    response = await client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={
            SIGNATURE_HEADER: sign(body, settings.security.whatsapp_app_secret),
            "content-type": "application/json",
        },
    )
    assert response.status_code == 202


async def _outbox_payloads(
    session_factory: async_sessionmaker[AsyncSession], event_type: str
) -> list[dict[str, Any]]:
    """The message bodies the publisher would put on the wire, in order."""
    async with session_factory() as session:
        rows = (await session.scalars(sa.select(OutboxEvent).order_by(OutboxEvent.id))).all()
    return [row.payload for row in rows if row.payload.get("event_type") == event_type]


async def test_three_fragments_reach_one_reply_through_real_message_bodies(
    client: httpx.AsyncClient,
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    scheduler = CapturingScheduler()
    aggregator = MessageAggregator(
        session_factory=session_factory,
        store=RedisDebounceStore(
            redis_client, absolute_window=migrated_database.workflow.max_batch_window
        ),
        scheduler=scheduler,
        settings=migrated_database,
    )
    worker = WorkflowWorker(session_factory=session_factory)

    for index, text in enumerate(["fiz supino", "3x10", "80kg"]):
        await _send(client, migrated_database, f"wamid.{index}", text)

    # Each stage is handed what the previous stage published, unmodified.
    for body in await _outbox_payloads(session_factory, "message.received"):
        await aggregator.on_message_received(body)

    batch_id = await aggregator.on_flush(scheduler.scheduled[-1])
    assert batch_id is not None

    for body in await _outbox_payloads(session_factory, INPUT_BATCH_READY):
        outcome = await worker.handle(body)

    assert outcome.executed

    async with session_factory() as session:
        batches = (await session.scalars(sa.select(MessageBatch))).all()
        executions = (await session.scalars(sa.select(WorkflowExecution))).all()
        outbound = (await session.scalars(sa.select(OutboundMessage))).all()

    assert len(batches) == 1, "three fragments, one batch"
    assert len(executions) == 1, "one workflow execution"
    assert len(outbound) == 1, "one reply"
    assert outbound[0].response_group_id == outcome.response_group_id


async def test_the_interaction_trace_survives_every_handoff(
    client: httpx.AsyncClient,
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    """Q131 across process boundaries: the trace minted at batch persistence is
    the one the reply carries."""
    scheduler = CapturingScheduler()
    aggregator = MessageAggregator(
        session_factory=session_factory,
        store=RedisDebounceStore(
            redis_client, absolute_window=migrated_database.workflow.max_batch_window
        ),
        scheduler=scheduler,
        settings=migrated_database,
    )
    worker = WorkflowWorker(session_factory=session_factory)

    await _send(client, migrated_database, "wamid.solo", "oi")
    for body in await _outbox_payloads(session_factory, "message.received"):
        await aggregator.on_message_received(body)
    await aggregator.on_flush(scheduler.scheduled[-1])
    for body in await _outbox_payloads(session_factory, INPUT_BATCH_READY):
        await worker.handle(body)

    async with session_factory() as session:
        batch = (await session.scalars(sa.select(MessageBatch))).one()
        execution = (await session.scalars(sa.select(WorkflowExecution))).one()
        outbound = (await session.scalars(sa.select(OutboundMessage))).one()

    assert batch.trace_id is not None
    assert execution.trace_id == batch.trace_id
    assert outbound.trace_id == batch.trace_id


async def test_a_consumer_rejects_a_body_that_is_not_an_envelope(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The defect this file exists to prevent: a flattened payload must fail
    loudly rather than look plausible."""
    from app.domain.events import EnvelopeDecodeError

    worker = WorkflowWorker(session_factory=session_factory)

    with pytest.raises(EnvelopeDecodeError):
        await worker.handle({"message_batch_id": str(UUID(int=1))})
