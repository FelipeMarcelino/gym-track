"""What every worker process needs, assembled once.

Kept in one place because the differences between the roles are genuinely small
-- a queue name and a handler -- and duplicating the setup in five files is how
five processes end up with three different retry policies.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractRobustConnection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import ApplicationSettings, ServiceName, load_settings
from app.infrastructure.postgres.engine import create_engine_for, create_session_factory
from app.infrastructure.rabbitmq.connection import connect, declare_topology, open_consumer_channel
from app.infrastructure.rabbitmq.retry import RetryPolicy, handle_with_retry
from app.infrastructure.rabbitmq.topology import build_topology
from app.observability import configure_logging

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, Any]]


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    settings: ApplicationSettings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    connection: AbstractRobustConnection
    channel: AbstractChannel


@asynccontextmanager
async def worker_runtime(service: ServiceName) -> AsyncIterator[WorkerRuntime]:
    """Settings, a database connection under this service's own role, a broker
    channel with QoS applied, and the topology declared."""
    settings = load_settings()
    configure_logging(settings.observability.log_level)

    engine = create_engine_for(settings, service)
    connection = await connect(settings.rabbitmq.url())
    try:
        channel = await open_consumer_channel(
            connection, prefetch=settings.rabbitmq.workflow_prefetch
        )
        # Declaring is idempotent, so every role can do it: a worker that starts
        # first does not have to wait for another process to have run.
        await declare_topology(
            channel, build_topology(settings.rabbitmq, partitions=settings.workflow.partitions)
        )
        logger.info("worker started", extra={"service": service.value})
        yield WorkerRuntime(
            settings=settings,
            engine=engine,
            session_factory=create_session_factory(engine),
            connection=connection,
            channel=channel,
        )
    finally:
        await connection.close()
        await engine.dispose()
        logger.info("worker stopped", extra={"service": service.value})


async def consume_forever(
    runtime: WorkerRuntime,
    queue_name: str,
    handler: MessageHandler,
) -> None:
    """Consume until the process is asked to stop.

    Bodies are decoded here and failures are routed by `handle_with_retry`, so
    a handler only ever sees a payload and only ever has to raise.
    """
    import json

    policy = RetryPolicy(runtime.settings.rabbitmq.retry_delays)
    queue = await runtime.channel.get_queue(queue_name, ensure=False)

    async def on_message(message: AbstractIncomingMessage) -> None:
        await handle_with_retry(
            message,
            lambda delivered: handler(json.loads(delivered.body)),
            channel=runtime.channel,
            queue=queue_name,
            policy=policy,
        )

    await queue.consume(on_message, no_ack=False)
    logger.info("consuming", extra={"queue": queue_name})
    await _until_stopped()


#: One stop event per process. A worker runs several consumers -- the
#: aggregator two, a workflow worker up to 32 -- and installing a handler per
#: consumer means each installation replaces the previous one, so SIGTERM wakes
#: only the last. Everything shares this event instead.
_stop_event: asyncio.Event | None = None
_stop_loop: asyncio.AbstractEventLoop | None = None


def shutdown_event() -> asyncio.Event:
    """The process-wide stop signal, with handlers installed exactly once."""
    global _stop_event, _stop_loop

    loop = asyncio.get_running_loop()
    if _stop_event is not None and _stop_loop is loop:
        return _stop_event

    _stop_event = asyncio.Event()
    _stop_loop = loop
    for received in (signal.SIGTERM, signal.SIGINT):
        # Not every platform supports signal handlers on the loop; a worker
        # that cannot listen for them still runs, it just stops less politely.
        with suppress(NotImplementedError):
            loop.add_signal_handler(received, _stop_event.set)
    return _stop_event


async def _until_stopped() -> None:
    """Block until SIGTERM or SIGINT.

    §37.4 wants a graceful stop: consumers stop receiving, in-flight work
    finishes its transaction, and unacked deliveries return to the queue for
    another worker rather than being lost.
    """
    await shutdown_event().wait()


def run(main: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Entrypoint wrapper: a clean exit on Ctrl-C rather than a traceback."""
    with suppress(KeyboardInterrupt):  # pragma: no cover - interactive only
        asyncio.run(main())
