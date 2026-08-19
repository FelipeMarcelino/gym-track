"""Publishing the delayed flush trigger (ADR-011, Q116).

The delay lives in the message's own expiration rather than in the queue's,
because a debounce delay depends on how much of the absolute window is left --
3s for a fresh window, less for one about to hit its cap. On expiry RabbitMQ
dead-letters the trigger from `debounce.delay` to `debounce.flush`.

Per-message TTL in a FIFO queue means a trigger with a short delay behind one
with a longer delay waits for the head to expire. Every delay here is at most
the sliding window, so the wait is bounded by that, and the stale-trigger rule
in `app.domain.debounce` is what keeps a late trigger harmless.
"""

from __future__ import annotations

from typing import Any

from aio_pika.abc import AbstractChannel

from app.infrastructure.rabbitmq.connection import build_message
from app.infrastructure.rabbitmq.topology import DEBOUNCE_DELAY_QUEUE, Exchanges


class RabbitMQFlushScheduler:
    """Publishes flush triggers into the delay queue."""

    def __init__(self, channel: AbstractChannel) -> None:
        self._channel = channel

    async def __call__(self, payload: dict[str, Any], *, delay_ms: int) -> None:
        exchange = await self._channel.get_exchange(Exchanges.DEBOUNCE, ensure=False)
        message = build_message(
            payload,
            idempotency_key=(
                f"flush:{payload['user_id']}:{payload['conversation_id']}:{payload['generation']}"
            ),
        )
        message.expiration = max(delay_ms, 1) / 1000
        await exchange.publish(message, routing_key=DEBOUNCE_DELAY_QUEUE)
