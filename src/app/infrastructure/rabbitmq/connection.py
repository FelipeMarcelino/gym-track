"""Declaring the topology against a live broker, and publishing through it."""

from __future__ import annotations

import json
import logging
from typing import Any, Final

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection

from app.application.ports.event_publisher import EventPublishError
from app.domain.events import DomainEventEnvelope
from app.infrastructure.rabbitmq.topology import Exchanges, Topology

logger = logging.getLogger(__name__)

#: Headers that must survive every hop, including a DLQ replay (Q117).
ATTEMPTS_HEADER: Final = "x-attempts"
IDEMPOTENCY_HEADER: Final = "x-idempotency-key"
ORIGIN_QUEUE_HEADER: Final = "x-origin-queue"
ORIGIN_EXCHANGE_HEADER: Final = "x-origin-exchange"
ORIGIN_ROUTING_KEY_HEADER: Final = "x-origin-routing-key"
FAILURE_REASON_HEADER: Final = "x-failure-reason"

_EXCHANGE_TYPES: Final[dict[str, aio_pika.ExchangeType]] = {
    "topic": aio_pika.ExchangeType.TOPIC,
    "direct": aio_pika.ExchangeType.DIRECT,
    "fanout": aio_pika.ExchangeType.FANOUT,
}


async def connect(url: str) -> AbstractRobustConnection:
    return await aio_pika.connect_robust(url)


async def declare_topology(channel: AbstractChannel, topology: Topology) -> None:
    """Apply the topology. Idempotent, so every worker can run it at start.

    Declaring is the only sane way to converge: a queue that exists with the
    same arguments is a no-op, and one that exists with *different* arguments
    fails loudly instead of silently running on stale settings.
    """
    exchanges: dict[str, AbstractExchange] = {}
    for spec in topology.exchanges:
        exchanges[spec.name] = await channel.declare_exchange(
            spec.name,
            _EXCHANGE_TYPES[spec.type],
            durable=spec.durable,
        )

    for queue_spec in topology.queues:
        queue = await channel.declare_queue(
            queue_spec.name,
            durable=queue_spec.durable,
            arguments=queue_spec.declaration_arguments() or None,
        )
        for binding in queue_spec.bindings:
            await queue.bind(exchanges[binding.exchange], routing_key=binding.routing_key)


class RabbitMQEventPublisher:
    """The outbox's way out (§27). Publishes and waits for broker confirmation.

    `publisher_confirms` is not optional: without it `publish` returns as soon
    as the bytes are written, and the outbox would mark rows PUBLISHED that the
    broker never accepted.
    """

    def __init__(self, channel: AbstractChannel) -> None:
        self._channel = channel

    async def publish(
        self,
        envelope: DomainEventEnvelope,
        *,
        exchange: str,
        routing_key: str,
    ) -> None:
        try:
            target = await self._channel.get_exchange(exchange, ensure=False)
            await target.publish(
                build_message(
                    envelope.model_dump(mode="json"),
                    idempotency_key=str(envelope.event_id),
                    correlation_id=envelope.correlation_id,
                    trace_id=envelope.trace_id,
                ),
                routing_key=routing_key,
            )
        except Exception as error:
            raise EventPublishError(f"publishing to {exchange}/{routing_key} failed") from error


def build_message(
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    correlation_id: str | None = None,
    trace_id: str | None = None,
    attempts: int = 0,
    headers: dict[str, Any] | None = None,
) -> aio_pika.Message:
    """A persistent message carrying the metadata every hop must preserve."""
    message_headers: dict[str, Any] = dict(headers or {})
    message_headers[IDEMPOTENCY_HEADER] = idempotency_key
    message_headers[ATTEMPTS_HEADER] = attempts
    if correlation_id is not None:
        message_headers["correlation_id"] = correlation_id
    if trace_id is not None:
        message_headers["trace_id"] = trace_id

    return aio_pika.Message(
        body=json.dumps(payload).encode(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        message_id=idempotency_key,
        correlation_id=correlation_id,
        headers=message_headers,
    )


def exchange_names() -> tuple[str, ...]:
    return (
        Exchanges.WHATSAPP_INBOUND,
        Exchanges.WORKFLOW,
        Exchanges.DOMAIN_EVENTS,
        Exchanges.BACKGROUND,
        Exchanges.WHATSAPP_OUTBOUND,
    )
