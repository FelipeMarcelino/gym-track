"""The `outbox-publisher` process (§27).

The only role that polls rather than consumes: its input is a database table,
not a queue. It sleeps between empty passes and keeps going while there is
work, so a burst drains at full speed and an idle system stays quiet.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from app.config import ServiceName
from app.entrypoints.runtime import run, shutdown_event, worker_runtime
from app.infrastructure.rabbitmq.connection import RabbitMQEventPublisher
from app.workers.outbox_publisher import OutboxPublisher

logger = logging.getLogger(__name__)

IDLE_INTERVAL_SECONDS = 0.5


async def publish_until_stopped(
    publisher: OutboxPublisher,
    stop: asyncio.Event,
    *,
    idle_interval: float = IDLE_INTERVAL_SECONDS,
) -> None:
    """Drain the outbox while there is work, and idle politely when there is not.

    The stop event is checked between passes rather than only slept on, so a
    SIGTERM during a burst still ends the loop and lets the runtime close its
    connections. Being killed mid-pass is survivable -- a row published but not
    yet marked is republished, and consumers deduplicate -- but it is needless.
    """
    while not stop.is_set():
        result = await publisher.publish_pending()
        if result.claimed:
            continue
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=idle_interval)


async def main() -> None:
    async with worker_runtime(ServiceName.OUTBOX_PUBLISHER) as runtime:
        await publish_until_stopped(
            OutboxPublisher(runtime.session_factory, RabbitMQEventPublisher(runtime.channel)),
            shutdown_event(),
        )


if __name__ == "__main__":  # pragma: no cover
    run(main)
