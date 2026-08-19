"""The `message-aggregator` process (§8, §10).

Two queues, one process: `message.received` registers fragments, and
`debounce.flush` closes windows into batches when their trigger expires.
"""

from __future__ import annotations

import asyncio

from app.config import ServiceName
from app.entrypoints.runtime import consume_forever, run, worker_runtime
from app.infrastructure.rabbitmq.scheduling import RabbitMQFlushScheduler
from app.infrastructure.rabbitmq.topology import DEBOUNCE_FLUSH_QUEUE
from app.infrastructure.redis.debounce import RedisDebounceStore
from app.workers.message_aggregator import FlushScheduler, MessageAggregator


async def main() -> None:
    from redis.asyncio import Redis

    async with worker_runtime(ServiceName.MESSAGE_AGGREGATOR) as runtime:
        redis = Redis.from_url(runtime.settings.redis.url())
        aggregator = MessageAggregator(
            session_factory=runtime.session_factory,
            store=RedisDebounceStore(
                redis, absolute_window=runtime.settings.workflow.max_batch_window
            ),
            scheduler=FlushScheduler(RabbitMQFlushScheduler(runtime.channel)),
            settings=runtime.settings,
        )
        try:
            await asyncio.gather(
                consume_forever(runtime, "message.received", aggregator.on_message_received),
                consume_forever(runtime, DEBOUNCE_FLUSH_QUEUE, aggregator.on_flush),
            )
        finally:
            await redis.aclose()


if __name__ == "__main__":  # pragma: no cover
    run(main)
