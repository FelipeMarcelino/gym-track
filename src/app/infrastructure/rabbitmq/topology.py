"""The broker topology, declared from data (§9.1, §9.4, Q111, Q114-Q117).

Everything the system needs from RabbitMQ is described here as values, then
applied. Two things follow from that:

* declaring is idempotent -- re-running it on a live broker is a no-op, which
  is what makes it safe to run at every worker start;
* the topology can be asserted in a test without a broker, because the
  description is separable from the act of declaring it.

Retries are queues, not sleeps (Q116). Each tier is a queue with a TTL and no
consumer; when a message expires it is dead-lettered back to the exchange that
routes it to its origin queue. A worker never holds a message while waiting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from app.config import RabbitMQSettings
from app.infrastructure.rabbitmq.partitioning import DEFAULT_PARTITIONS, partition_queue_name


class Exchanges:
    """§9.1. Separated by responsibility, so a consumer binds to what it owns."""

    WHATSAPP_INBOUND: Final = "whatsapp.inbound"
    WORKFLOW: Final = "workflow"
    DOMAIN_EVENTS: Final = "domain.events"
    BACKGROUND: Final = "background"
    WHATSAPP_OUTBOUND: Final = "whatsapp.outbound"

    #: Where expired retry messages are dead-lettered back to, and where the
    #: dead-letter queue itself is bound. Kept apart from the business
    #: exchanges so retry traffic never matches a business binding by accident.
    RETRY: Final = "retry"
    DEAD_LETTER: Final = "dead-letter"


@dataclass(frozen=True, slots=True)
class ExchangeSpec:
    name: str
    type: str = "topic"
    durable: bool = True


@dataclass(frozen=True, slots=True)
class Binding:
    exchange: str
    routing_key: str


@dataclass(frozen=True, slots=True)
class QueueSpec:
    name: str
    bindings: tuple[Binding, ...]
    durable: bool = True
    #: Q114: one consumer at a time per partition, so per-user ordering holds
    #: even while several workers are running.
    single_active_consumer: bool = False
    arguments: dict[str, Any] = field(default_factory=dict)

    def declaration_arguments(self) -> dict[str, Any]:
        arguments = dict(self.arguments)
        if self.single_active_consumer:
            arguments["x-single-active-consumer"] = True
        return arguments


@dataclass(frozen=True, slots=True)
class Topology:
    exchanges: tuple[ExchangeSpec, ...]
    queues: tuple[QueueSpec, ...]

    def queue(self, name: str) -> QueueSpec:
        for spec in self.queues:
            if spec.name == name:
                return spec
        raise KeyError(name)


def retry_queue_name(queue: str, tier: int) -> str:
    return f"{queue}.retry.{tier}"


def dead_letter_queue_name(queue: str) -> str:
    return f"{queue}.dlq"


def _retry_and_dlq_specs(queue: str, delays: tuple[timedelta, ...]) -> list[QueueSpec]:
    """A TTL queue per tier, plus the terminal dead-letter queue.

    A message that expires in tier N is dead-lettered with its original routing
    key, which is what sends it back to `queue` rather than to the next tier.
    """
    specs = [
        QueueSpec(
            name=retry_queue_name(queue, tier),
            bindings=(Binding(Exchanges.RETRY, retry_queue_name(queue, tier)),),
            arguments={
                "x-message-ttl": int(delay.total_seconds() * 1000),
                "x-dead-letter-exchange": Exchanges.RETRY,
                "x-dead-letter-routing-key": queue,
            },
        )
        for tier, delay in enumerate(delays, start=1)
    ]
    specs.append(
        QueueSpec(
            name=dead_letter_queue_name(queue),
            bindings=(Binding(Exchanges.DEAD_LETTER, dead_letter_queue_name(queue)),),
        )
    )
    return specs


def build_topology(
    settings: RabbitMQSettings,
    *,
    partitions: int = DEFAULT_PARTITIONS,
) -> Topology:
    exchanges = [
        ExchangeSpec(Exchanges.WHATSAPP_INBOUND),
        ExchangeSpec(Exchanges.WORKFLOW, type="direct"),
        ExchangeSpec(Exchanges.DOMAIN_EVENTS),
        ExchangeSpec(Exchanges.BACKGROUND),
        ExchangeSpec(Exchanges.WHATSAPP_OUTBOUND),
        ExchangeSpec(Exchanges.RETRY, type="direct"),
        ExchangeSpec(Exchanges.DEAD_LETTER, type="direct"),
    ]

    queues: list[QueueSpec] = []

    # The partitioned workflow queues: the ordering contract of §9.2.
    for partition in range(partitions):
        name = partition_queue_name(partition, partitions)
        queues.append(
            QueueSpec(
                name=name,
                # The second binding is how an expired retry message finds its
                # way home: tiers dead-letter to the retry exchange with the
                # origin queue's name as the routing key.
                bindings=(
                    Binding(Exchanges.WORKFLOW, name),
                    Binding(Exchanges.RETRY, name),
                ),
                single_active_consumer=True,
            )
        )

    # One work queue per remaining consumer class.
    for name, exchange, routing_key in (
        ("message.received", Exchanges.WHATSAPP_INBOUND, "message.received"),
        ("outbound.dispatch", Exchanges.WHATSAPP_OUTBOUND, "outbound.#"),
        ("background.jobs", Exchanges.BACKGROUND, "#"),
    ):
        queues.append(
            QueueSpec(
                name=name,
                bindings=(Binding(exchange, routing_key), Binding(Exchanges.RETRY, name)),
            )
        )

    # Every queue that carries business work gets retry tiers and a DLQ (Q117).
    for spec in list(queues):
        queues.extend(_retry_and_dlq_specs(spec.name, settings.retry_delays))

    return Topology(exchanges=tuple(exchanges), queues=tuple(queues))
