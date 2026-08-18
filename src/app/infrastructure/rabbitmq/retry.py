"""Asynchronous retries through delay queues (§9.4, Q116).

A consumer never sleeps. On failure the message is republished into the tier
queue whose TTL matches the next delay and the original delivery is acked; when
the TTL expires, RabbitMQ dead-letters the message back to the origin queue.
The worker holds no message and no consumer slot while waiting, which is the
entire reason §9.3 forbids sleeping inside a consumer.

After the last tier the message goes to the queue's DLQ with its original
routing information and idempotency key intact, so replay tooling can put it
back exactly as it arrived (Q117).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from aio_pika.abc import AbstractChannel, AbstractIncomingMessage

from app.infrastructure.rabbitmq.connection import (
    ATTEMPTS_HEADER,
    FAILURE_REASON_HEADER,
    IDEMPOTENCY_HEADER,
    ORIGIN_EXCHANGE_HEADER,
    ORIGIN_QUEUE_HEADER,
    ORIGIN_ROUTING_KEY_HEADER,
    build_message,
)
from app.infrastructure.rabbitmq.topology import (
    Exchanges,
    dead_letter_queue_name,
    retry_queue_name,
)

logger = logging.getLogger(__name__)

Handler = Callable[[AbstractIncomingMessage], Awaitable[None]]


def attempts_of(message: AbstractIncomingMessage) -> int:
    raw = (message.headers or {}).get(ATTEMPTS_HEADER, 0)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


class RetryPolicy:
    """Decides where a failed message goes next: a tier, or the DLQ."""

    def __init__(self, delays: tuple[timedelta, ...]) -> None:
        if not delays:
            raise ValueError("at least one retry tier is required")
        self._delays = delays

    @property
    def tiers(self) -> int:
        return len(self._delays)

    def next_tier(self, attempts: int) -> int | None:
        """Tier number for the next delivery, or None when the DLQ is next."""
        return attempts + 1 if attempts < self._delays.__len__() else None

    def delay_for(self, tier: int) -> timedelta:
        return self._delays[tier - 1]


async def handle_with_retry(
    message: AbstractIncomingMessage,
    handler: Handler,
    *,
    channel: AbstractChannel,
    queue: str,
    policy: RetryPolicy,
) -> bool:
    """Run the handler; on failure route the message onward. Returns success.

    The original delivery is always acked -- the message is not lost, it has
    been moved. Nacking instead would either requeue it in a hot loop or
    dead-letter it immediately, and neither is the tiered schedule §9.4 asks
    for.
    """
    try:
        await handler(message)
    except Exception as error:
        await _route_failure(message, channel=channel, queue=queue, policy=policy, error=error)
        await message.ack()
        return False

    await message.ack()
    return True


async def _route_failure(
    message: AbstractIncomingMessage,
    *,
    channel: AbstractChannel,
    queue: str,
    policy: RetryPolicy,
    error: Exception,
) -> None:
    attempts = attempts_of(message)
    headers = dict(message.headers or {})
    idempotency_key = str(headers.get(IDEMPOTENCY_HEADER) or message.message_id or "")

    # Recorded on the first hop only, so a replayed message still knows where
    # it originally came from.
    headers.setdefault(ORIGIN_QUEUE_HEADER, queue)
    headers.setdefault(ORIGIN_EXCHANGE_HEADER, message.exchange)
    headers.setdefault(ORIGIN_ROUTING_KEY_HEADER, message.routing_key)
    headers[FAILURE_REASON_HEADER] = repr(error)[:500]

    tier = policy.next_tier(attempts)
    if tier is None:
        destination_exchange = Exchanges.DEAD_LETTER
        routing_key = dead_letter_queue_name(queue)
        logger.error(
            "message exhausted its retries and moved to the DLQ",
            extra={"queue": queue, "attempts": attempts, "idempotency_key": idempotency_key},
        )
    else:
        destination_exchange = Exchanges.RETRY
        routing_key = retry_queue_name(queue, tier)
        logger.warning(
            "message scheduled for retry",
            extra={
                "queue": queue,
                "tier": tier,
                "delay_seconds": policy.delay_for(tier).total_seconds(),
                "attempts": attempts,
                "idempotency_key": idempotency_key,
            },
        )

    exchange = await channel.get_exchange(destination_exchange, ensure=False)
    await exchange.publish(
        build_message(
            _decoded_payload(message),
            idempotency_key=idempotency_key,
            correlation_id=message.correlation_id,
            trace_id=str(headers.get("trace_id")) if headers.get("trace_id") else None,
            attempts=attempts + 1,
            headers=headers,
        ),
        routing_key=routing_key,
    )


def _decoded_payload(message: AbstractIncomingMessage) -> dict[str, object]:
    import json

    decoded = json.loads(message.body)
    if not isinstance(decoded, dict):
        raise TypeError("message body is not a JSON object")
    return decoded
