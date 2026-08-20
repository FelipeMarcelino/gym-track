"""A whole walking skeleton, wired through real infrastructure.

Every stage here is driven by messages that actually travelled through
RabbitMQ, rows that were actually written, and a broker topology that was
actually declared. The only fake is the WhatsApp provider (D6).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.app import create_app
from app.api.dependencies import ApiContext
from app.application.ports.event_publisher import EventPublisher
from app.application.services.training_sessions import TrainingSessionManager
from app.application.services.workout_command_builder import SessionScopedCommandBuilder
from app.application.services.workout_logging import WorkoutApplicationService
from app.config import ApplicationSettings
from app.graphs.main.handlers import build_task_handlers
from app.infrastructure.rabbitmq.connection import RabbitMQEventPublisher, connect
from app.infrastructure.rabbitmq.scheduling import RabbitMQFlushScheduler
from app.infrastructure.rabbitmq.topology import (
    DEBOUNCE_DELAY_QUEUE,
    DEBOUNCE_FLUSH_QUEUE,
    build_topology,
)
from app.infrastructure.redis.debounce import RedisDebounceStore
from app.infrastructure.whatsapp.fake_client import FakeWhatsAppClient
from app.security.signatures import SIGNATURE_HEADER, sign
from app.workers.dispatcher import WhatsAppDispatcher
from app.workers.message_aggregator import FlushScheduler, MessageAggregator
from app.workers.outbox_publisher import OutboxPublisher
from app.workers.workflow_worker import WorkflowWorker
from tests.conftest import redeclare_topology

BSUID = "5511987654321"

#: Queues the skeleton actually uses, purged between tests.
WORKED_QUEUES = (
    "message.received",
    DEBOUNCE_FLUSH_QUEUE,
    DEBOUNCE_DELAY_QUEUE,
    "outbound.dispatch",
)


@dataclass
class Skeleton:
    """Every component of the pipeline, sharing one set of containers."""

    settings: ApplicationSettings
    client: httpx.AsyncClient
    channel: AbstractChannel
    publisher: OutboxPublisher
    aggregator: MessageAggregator
    worker: WorkflowWorker
    dispatcher: WhatsAppDispatcher
    whatsapp: FakeWhatsAppClient
    partitions: int
    #: The workout domain, wired the way the worker entrypoint wires it, so the
    #: e2e path exercises the real registry rather than a test-only one.
    workout: WorkoutApplicationService
    session_factory: async_sessionmaker[AsyncSession]

    async def rows(self, statement: str, **params: Any) -> list[Any]:
        """Read the database from outside the components that wrote it.

        The point of an end-to-end test is what is *there* afterwards, and a
        component reporting on its own work can be wrong in exactly the way the
        test exists to catch.
        """
        async with self.session_factory() as session:
            return list((await session.execute(sa.text(statement), params)).all())

    async def send_webhook(self, external_id: str, text: str) -> httpx.Response:
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
        return await self.client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                SIGNATURE_HEADER: sign(body, self.settings.security.whatsapp_app_secret),
                "content-type": "application/json",
            },
        )

    async def publish_outbox(self) -> int:
        """Drain the outbox onto the broker, the way the publisher process does."""
        return (await self.publisher.publish_pending()).published

    async def drain(
        self,
        queue_name: str,
        handler: Callable[[dict[str, Any]], Any],
        *,
        limit: int = 10,
    ) -> int:
        """Consume up to `limit` messages from a real queue and hand them over."""
        queue = await self.channel.get_queue(queue_name, ensure=False)
        handled = 0

        while handled < limit:
            message: AbstractIncomingMessage | None = await queue.get(fail=False)
            if message is None:
                break
            await handler(json.loads(message.body))
            await message.ack()
            handled += 1

        return handled


@pytest.fixture
async def skeleton(
    migrated_database: ApplicationSettings,
    admin_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    rabbitmq_url: str,
    redis_url: str,
) -> AsyncIterator[Skeleton]:
    # The real partition count: the aggregator routes with the value from
    # settings, so a topology declared with any other number silently drops
    # every InputBatchReady into an unbound routing key.
    partitions = migrated_database.workflow.partitions

    redis = Redis.from_url(redis_url)
    await redis.flushall()

    workout = WorkoutApplicationService(
        session_factory=session_factory,
        sessions=TrainingSessionManager(
            timeout=migrated_database.workflow.training_session_timeout
        ),
    )

    connection = await connect(rabbitmq_url)
    channel = await connection.channel(publisher_confirms=True)
    await redeclare_topology(
        channel, build_topology(migrated_database.rabbitmq, partitions=partitions)
    )

    app = create_app(
        ApiContext(settings=migrated_database, engine=admin_engine, session_factory=session_factory)
    )
    transport = httpx.ASGITransport(app=app)
    whatsapp = FakeWhatsAppClient()

    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        publisher: EventPublisher = RabbitMQEventPublisher(channel)
        yield Skeleton(
            settings=migrated_database,
            client=client,
            channel=channel,
            publisher=OutboxPublisher(session_factory, publisher),
            aggregator=MessageAggregator(
                session_factory=session_factory,
                store=RedisDebounceStore(
                    redis, absolute_window=migrated_database.workflow.max_batch_window
                ),
                scheduler=FlushScheduler(RabbitMQFlushScheduler(channel)),
                settings=migrated_database,
            ),
            worker=WorkflowWorker(
                session_factory=session_factory,
                handlers=build_task_handlers(
                    workout=workout, builder=SessionScopedCommandBuilder(session_factory)
                ),
            ),
            dispatcher=WhatsAppDispatcher(
                session_factory=session_factory,
                client=whatsapp,
                settings=migrated_database,
            ),
            whatsapp=whatsapp,
            partitions=partitions,
            workout=workout,
            session_factory=session_factory,
        )

    await redis.aclose()
    await connection.close()
