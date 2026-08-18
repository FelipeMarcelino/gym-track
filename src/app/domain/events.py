"""The domain event envelope (§27.1).

This is a wire contract: it crosses a process boundary and outlives the code
that wrote it, because a message published today can be consumed by a version
of the consumer deployed tomorrow. The contract test in tests/contract freezes
its serialized shape against a golden fixture for that reason.

`event_id` is the deduplication key. §27 is explicit that duplicate publication
stays possible around failures, so consumers deduplicate on this value -- which
is also the primary key of the `domain_events` row, so the two cannot drift.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.identifiers import new_uuid7


def _now() -> datetime:
    return datetime.now(UTC)


class DomainEventEnvelope(BaseModel):
    """One thing that happened, with the metadata needed to trace and dedupe it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=new_uuid7)
    event_type: str
    event_version: int = 1
    aggregate_type: str
    aggregate_id: UUID
    user_id: UUID | None = None
    trace_id: str | None = None
    correlation_id: str | None = None
    #: The event or command that caused this one, for reconstructing a chain.
    causation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=_now)

    def with_correlation(
        self,
        *,
        trace_id: str | None,
        correlation_id: str | None,
        causation_id: str | None = None,
    ) -> Self:
        return self.model_copy(
            update={
                "trace_id": trace_id,
                "correlation_id": correlation_id,
                "causation_id": causation_id or self.causation_id,
            }
        )
