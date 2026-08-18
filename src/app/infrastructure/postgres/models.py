"""Migration 0001's tables: the durability spine of the walking skeleton (§26).

Only the eleven tables the pipeline actually touches this sprint are here. The
rest of §26 -- training, catalog, programs, memory, knowledge -- arrives with
the domain that needs it, because a table nothing writes to is a guess that
ages badly.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.postgres.base import Base, SoftDeleteMixin, enum_column


class UserStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"


class MessagingProvider(StrEnum):
    WHATSAPP = "whatsapp"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageContentType(StrEnum):
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    UNSUPPORTED = "unsupported"


class MessageBatchStatus(StrEnum):
    OPEN = "open"
    FLUSHED = "flushed"


class WorkflowExecutionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DeliveryState(StrEnum):
    """Ordered on purpose: WS-10 asserts transitions never move backwards."""

    PENDING = "pending"
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    DELIVERED = "delivered"
    FAILED = "failed"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"


class User(Base, SoftDeleteMixin):
    __tablename__ = "users"

    locale: Mapped[str] = mapped_column(sa.String(16), default="pt-BR", nullable=False)
    timezone: Mapped[str] = mapped_column(sa.String(64), default="UTC", nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus), default=UserStatus.ACTIVE, nullable=False
    )


class UserIdentifier(Base):
    """External identity, stored as ciphertext plus a keyed lookup hash (Q143).

    The HMAC is what queries match on, so an exact lookup never requires
    decrypting anything and the plaintext identifier is never searchable.
    """

    __tablename__ = "user_identifiers"
    __table_args__ = (
        sa.UniqueConstraint(
            "provider", "external_id_lookup_hmac", name="uq_user_identifiers_provider_lookup"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[MessagingProvider] = mapped_column(
        enum_column(MessagingProvider), nullable=False
    )
    external_id_ciphertext: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    external_id_lookup_hmac: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)


class Conversation(Base, SoftDeleteMixin):
    """A conversational window; rotates after inactivity (§7.2, Q30)."""

    __tablename__ = "conversations"

    user_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[ConversationStatus] = mapped_column(
        enum_column(ConversationStatus), default=ConversationStatus.ACTIVE, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class Message(Base):
    """One inbound or outbound message. Dedupe lives on the unique key (§28)."""

    __tablename__ = "messages"
    __table_args__ = (
        sa.UniqueConstraint(
            "provider", "external_message_id", name="uq_messages_provider_external_id"
        ),
        sa.Index("ix_messages_conversation_received", "conversation_id", "received_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[MessagingProvider] = mapped_column(
        enum_column(MessagingProvider), nullable=False
    )
    external_message_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    direction: Mapped[MessageDirection] = mapped_column(
        enum_column(MessageDirection), nullable=False
    )
    content_type: Mapped[MessageContentType] = mapped_column(
        enum_column(MessageContentType), default=MessageContentType.TEXT, nullable=False
    )
    text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    provider_sent_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    # The trace of the webhook request that persisted this row. The interaction
    # trace is minted later, by the aggregator, and links back to these (Q131).
    trace_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class MessageBatch(Base):
    """A debounce aggregation: one workflow input (§8, §10)."""

    __tablename__ = "message_batches"
    __table_args__ = (
        sa.Index("ix_message_batches_conversation_status", "conversation_id", "status"),
    )

    user_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[MessageBatchStatus] = mapped_column(
        enum_column(MessageBatchStatus), default=MessageBatchStatus.OPEN, nullable=False
    )
    # The debounce generation this batch was flushed for; a delayed flush
    # carrying a stale generation must not act on it (Q113, ADR-011).
    generation: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    flushed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # The single interaction trace for this batch, minted at persistence (Q131).
    trace_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class MessageBatchItem(Base):
    """Membership of a message in a batch, in arrival order."""

    __tablename__ = "message_batch_items"
    __table_args__ = (
        sa.UniqueConstraint("message_batch_id", "message_id", name="uq_batch_items_batch_message"),
        sa.UniqueConstraint("message_batch_id", "position", name="uq_batch_items_batch_position"),
    )

    message_batch_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("message_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class WorkflowExecution(Base):
    """One execution per batch: redelivery resumes it, never duplicates it (§28)."""

    __tablename__ = "workflow_executions"
    __table_args__ = (sa.UniqueConstraint("message_batch_id", name="uq_workflow_executions_batch"),)

    message_batch_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("message_batches.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[WorkflowExecutionStatus] = mapped_column(
        enum_column(WorkflowExecutionStatus),
        default=WorkflowExecutionStatus.RUNNING,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class ProcessedOperation(Base):
    """Idempotency ledger for domain commands (§28).

    A replayed message re-runs the handler; this table is what stops the second
    run from producing a second business effect.
    """

    __tablename__ = "processed_operations"
    __table_args__ = (
        sa.UniqueConstraint("operation_id", name="uq_processed_operations_operation_id"),
    )

    operation_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    operation_type: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    workflow_execution_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("workflow_executions.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    processed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class OutboundMessage(Base):
    """A reply awaiting dispatch, ordered within its response group (§25)."""

    __tablename__ = "outbound_messages"
    __table_args__ = (
        sa.UniqueConstraint(
            "response_group_id", "sequence", name="uq_outbound_messages_group_sequence"
        ),
        sa.Index("ix_outbound_messages_state", "delivery_state", "response_group_id", "sequence"),
    )

    user_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    workflow_execution_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("workflow_executions.id", ondelete="SET NULL"), nullable=True
    )
    response_group_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    delivery_state: Mapped[DeliveryState] = mapped_column(
        enum_column(DeliveryState), default=DeliveryState.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class DomainEvent(Base):
    """Append-only record of something that happened (§27.1).

    `id` is the envelope's `event_id`: consumers deduplicate on it, so the two
    must be the same value rather than two identifiers that agree by convention.
    """

    __tablename__ = "domain_events"
    __table_args__ = (sa.Index("ix_domain_events_aggregate", "aggregate_type", "aggregate_id"),)

    event_type: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    event_version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    trace_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class OutboxEvent(Base):
    """The publication half of DEC-005, written in the caller's transaction (§27)."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        sa.UniqueConstraint("domain_event_id", name="uq_outbox_events_domain_event"),
        sa.Index("ix_outbox_events_claimable", "status", "available_at"),
    )

    domain_event_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("domain_events.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[OutboxStatus] = mapped_column(
        enum_column(OutboxStatus), default=OutboxStatus.PENDING, nullable=False
    )
    exchange: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    routing_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
