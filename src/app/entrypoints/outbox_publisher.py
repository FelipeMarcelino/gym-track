"""The `outbox-publisher` process (§27).

The only role that polls rather than consumes: its input is a database table,
not a queue. It sleeps between empty passes and keeps going while there is
work, so a burst drains at full speed and an idle system stays quiet.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import ServiceName
from app.entrypoints.runtime import run, worker_runtime
from app.infrastructure.rabbitmq.connection import RabbitMQEventPublisher
from app.workers.outbox_publisher import OutboxPublisher

logger = logging.getLogger(__name__)

IDLE_INTERVAL_SECONDS = 0.5


async def main() -> None:
    async with worker_runtime(ServiceName.OUTBOX_PUBLISHER) as runtime:
        publisher = OutboxPublisher(
            runtime.session_factory, RabbitMQEventPublisher(runtime.channel)
        )

        while True:
            result = await publisher.publish_pending()
            if result.claimed == 0:
                await asyncio.sleep(IDLE_INTERVAL_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    run(main)
