"""The port the outbox publishes through (§27).

Deliberately narrow: publish one envelope and do not return until the broker
has confirmed it. The publisher marks a row PUBLISHED on that return, so an
implementation that returns before confirmation would turn at-least-once into
at-most-once, silently.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.events import DomainEventEnvelope


class EventPublishError(RuntimeError):
    """Publication failed; the outbox row stays PENDING and is retried."""


class EventPublisher(Protocol):
    async def publish(
        self,
        envelope: DomainEventEnvelope,
        *,
        exchange: str,
        routing_key: str,
    ) -> None:
        """Publish and await broker confirmation, or raise EventPublishError."""
        ...
