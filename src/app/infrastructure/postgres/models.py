"""Migration 0001's tables: the durability spine of the walking skeleton (§26).

Only the eleven tables the pipeline actually touches this sprint are here. The
rest of §26 -- training, catalog, programs, memory, knowledge -- arrives with
the domain that needs it, because a table nothing writes to is a guess that
ages badly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.clarification.status import ClarificationReason, ClarificationStatus
from app.domain.exercises.catalog import AliasSource, ExerciseRelationType, MuscleRole
from app.domain.results import ResultVisibility, TaskType
from app.domain.training.activities import ActivityType, LoadMode, SetType
from app.domain.training.effort import EffortMethod
from app.domain.training.provenance import ExerciseGroupType, Provenance, SourceRole
from app.domain.workflow.tasks import TaskStatus
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
    """How one delivery of one batch ended.

    `WAITING_FOR_USER` and `PARTIAL_SUCCESS` are *committed outcomes*, not
    failures (Q28): a workflow that asked a question or recorded half of a
    mixed batch did its job, and a redelivery of it must produce no second
    effect. Only RUNNING (a crash mid-flight) and FAILED are re-run.
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WAITING_FOR_USER = "waiting_for_user"
    PARTIAL_SUCCESS = "partial_success"


#: Outcomes that mean this delivery finished and its work is durable. Paired
#: with `finished_at` by a CHECK -- `WAITING_FOR_USER` is deliberately absent,
#: because a paused execution has not finished and stamping it would make "how
#: long do users take to answer" unanswerable.
TERMINAL_WORKFLOW_STATUSES: frozenset[WorkflowExecutionStatus] = frozenset(
    {
        WorkflowExecutionStatus.SUCCEEDED,
        WorkflowExecutionStatus.FAILED,
        WorkflowExecutionStatus.PARTIAL_SUCCESS,
    }
)


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
    __table_args__ = (
        # One active conversation per user, enforced by the database. Two
        # concurrent messages arriving after a timeout would otherwise both
        # rotate, splitting one user's fragments across two threads.
        sa.Index(
            "uq_conversations_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
        ),
    )

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
    # The provider's handle for attached media. Without it an accepted audio
    # message is unusable: speech-to-text has nothing to fetch, and the webhook
    # has already returned 202.
    provider_media_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
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
        sa.UniqueConstraint("message_batch_id", "position", name="uq_batch_items_batch_position"),
        # A message belongs to at most one batch, ever. Without this, two
        # concurrent flushes could each build a batch from the same fragments
        # and produce two workflow executions for one interaction.
        sa.UniqueConstraint("message_id", name="uq_batch_items_message"),
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
    __table_args__ = (
        sa.UniqueConstraint("message_batch_id", name="uq_workflow_executions_batch"),
        # Terminality and its timestamp are two statements of the same fact.
        # A SUCCEEDED row with no `finished_at` makes every duration query
        # silently wrong, and `WAITING_FOR_USER` must *not* be stamped.
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'partial_success')) = (finished_at IS NOT NULL)",
            name="ck_workflow_executions_finished_when_terminal",
        ),
    )

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
        # Named explicitly. Migration 0011 had to drop SQLAlchemy's
        # auto-generated `workflowexecutionstatus` to widen the vocabulary, and
        # a model that still asked for the old name would leave the schema and
        # the metadata disagreeing -- which the drift test catches, loudly.
        enum_column(WorkflowExecutionStatus, name="ck_workflow_executions_status"),
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
    #: Q132. A column rather than checkpoint state, because ADR-015 says the
    #: checkpoint is not authoritative and may be pruned -- traceability that
    #: lives only there disappears with it. Nullable: rows written before this
    #: column existed have no honest value for it.
    graph_version: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    #: The paused execution this one resumed, when it is an answer to a
    #: clarification. An answer is its own `MessageBatch` and therefore its own
    #: execution; without this link the pair is unrecoverable afterwards.
    resumed_execution_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("workflow_executions.id", ondelete="SET NULL"), nullable=True
    )


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


# ---------------------------------------------------------------------------
# Exercise catalog (§16, Q43, Q44) — Sprint 2, WS-1
# ---------------------------------------------------------------------------


class Exercise(Base, SoftDeleteMixin):
    """One canonical movement. The catalog is global, not per user (§16)."""

    __tablename__ = "exercises"
    __table_args__ = (
        sa.UniqueConstraint("canonical_name", name="uq_exercises_canonical_name"),
        sa.UniqueConstraint("slug", name="uq_exercises_slug"),
    )

    canonical_name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    #: Stable join key for the seed. Renaming a display name must not orphan
    #: the muscles and aliases attached to it.
    slug: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    activity_type: Mapped[ActivityType] = mapped_column(enum_column(ActivityType), nullable=False)
    #: What a bare load means for this exercise (Q49). A dumbbell movement says
    #: PER_IMPLEMENT here, so WS-8 reads the catalog instead of guessing from
    #: the name.
    default_load_mode: Mapped[LoadMode] = mapped_column(enum_column(LoadMode), nullable=False)
    is_bodyweight: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    locale: Mapped[str] = mapped_column(sa.String(16), default="pt-BR", nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class ExerciseAlias(Base, SoftDeleteMixin):
    """A name people actually use for an exercise (§16).

    A null `user_id` is a global alias shipped with the catalog; a non-null one
    is learned from a user's clarification in Sprint 3 and belongs only to them.
    """

    __tablename__ = "exercise_aliases"
    __table_args__ = (
        # Two partial indexes rather than one composite: PostgreSQL treats
        # NULLs as distinct, so `UNIQUE(user_id, normalized_alias)` alone would
        # accept two *global* aliases with the same text -- and stage 2 of the
        # resolver would then have to choose between them.
        sa.Index(
            "uq_exercise_aliases_global",
            "normalized_alias",
            unique=True,
            postgresql_where=sa.text("user_id IS NULL AND deleted_at IS NULL"),
        ),
        sa.Index(
            "uq_exercise_aliases_user",
            "user_id",
            "normalized_alias",
            unique=True,
            postgresql_where=sa.text("user_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        sa.Index("ix_exercise_aliases_normalized", "normalized_alias"),
    )

    exercise_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("exercises.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    alias: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    #: What the resolver matches on: casefolded, unaccented, whitespace
    #: collapsed. Stored rather than computed so the match is an index seek.
    normalized_alias: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    source: Mapped[AliasSource] = mapped_column(
        enum_column(AliasSource), default=AliasSource.SEED, nullable=False
    )


class Muscle(Base):
    __tablename__ = "muscles"
    __table_args__ = (sa.UniqueConstraint("slug", name="uq_muscles_slug"),)

    name: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    slug: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    muscle_group: Mapped[str] = mapped_column(sa.String(80), nullable=False)


class ExerciseMuscle(Base):
    """Which muscles an exercise works, and how (Q43)."""

    __tablename__ = "exercise_muscles"
    __table_args__ = (
        sa.UniqueConstraint("exercise_id", "muscle_id", name="uq_exercise_muscles_pair"),
    )

    exercise_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("exercises.id", ondelete="CASCADE"), nullable=False, index=True
    )
    muscle_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("muscles.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MuscleRole] = mapped_column(enum_column(MuscleRole), nullable=False)


class Equipment(Base):
    __tablename__ = "equipment"
    __table_args__ = (sa.UniqueConstraint("slug", name="uq_equipment_slug"),)

    name: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    slug: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    #: Held one per hand. This is what makes PER_IMPLEMENT mechanical (Q49)
    #: instead of a pattern match on the word "dumbbell".
    is_implement: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)


class ExerciseEquipment(Base):
    __tablename__ = "exercise_equipment"
    __table_args__ = (
        sa.UniqueConstraint("exercise_id", "equipment_id", name="uq_exercise_equipment_pair"),
    )

    exercise_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("exercises.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipment_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)


class ExerciseRelation(Base):
    """How two exercises relate (Q44). Directional: from -> to."""

    __tablename__ = "exercise_relations"
    __table_args__ = (
        sa.UniqueConstraint(
            "from_exercise_id", "to_exercise_id", "relation_type", name="uq_exercise_relations"
        ),
        sa.CheckConstraint(
            "from_exercise_id <> to_exercise_id", name="ck_exercise_relations_not_self"
        ),
    )

    from_exercise_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("exercises.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_exercise_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("exercises.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[ExerciseRelationType] = mapped_column(
        enum_column(ExerciseRelationType), nullable=False
    )


# ---------------------------------------------------------------------------
# Training sessions and the audit trail (§18, §15, §26) — Sprint 2, WS-5
# ---------------------------------------------------------------------------


class TrainingSessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class ActorType(StrEnum):
    """Who caused a mutation.

    SYSTEM is not decoration: a session closed by the background sweep has no
    user behind it, and recording one would attribute an action to somebody who
    was not there.
    """

    USER = "user"
    SYSTEM = "system"
    OPERATOR = "operator"


class TrainingSession(Base, SoftDeleteMixin):
    """A workout, started by logging and closed by inactivity (§18).

    Distinct from a conversation: a conversation is a window of talking, this
    is a window of training, and §7.2 keeps their timeouts independent.
    """

    __tablename__ = "training_sessions"
    __table_args__ = (
        # One open session per user, enforced by the database. Two concurrent
        # logs would otherwise each open one, and the user's sets would land in
        # two workouts -- the Sprint 1 lesson from conversations, applied here
        # before it can happen rather than after.
        sa.Index(
            "uq_training_sessions_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
        ),
        sa.Index("ix_training_sessions_expiry", "status", "last_activity_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[TrainingSessionStatus] = mapped_column(
        enum_column(TrainingSessionStatus),
        default=TrainingSessionStatus.ACTIVE,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    #: §18 makes this the authority. A Redis hint may suggest a session looks
    #: stale; only this column decides.
    last_activity_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    closed_by: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    #: Optimistic concurrency for Sprint 4's corrections (§17). Nothing writes
    #: it yet; the column ships now so the correction sprint is additive.
    expected_version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class AuditEvent(Base):
    """Who caused a change (§15, §26). Append-only.

    Not a second copy of `domain_events`. An event says what happened so
    consumers can react; an audit row says who caused it. They answer different
    questions when somebody asks why a set exists.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        sa.Index("ix_audit_events_entity", "entity_type", "entity_id"),
        sa.Index("ix_audit_events_actor", "actor_user_id", "occurred_at"),
    )

    actor_type: Mapped[ActorType] = mapped_column(enum_column(ActorType), nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    workflow_execution_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("workflow_executions.id", ondelete="SET NULL"), nullable=True
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SessionExercise(Base, SoftDeleteMixin):
    """One exercise as performed inside a session (Q58).

    A block rather than "the exercise in this workout": coming back to the
    bench press after squats is a second block, and the index preserves the
    order it happened in. Grouping by exercise would answer "what did I do
    first" with an ordering the user never performed.
    """

    __tablename__ = "session_exercises"
    __table_args__ = (
        sa.UniqueConstraint(
            "training_session_id", "exercise_block_index", name="uq_session_exercises_block"
        ),
        # The group is referenced together with the session it belongs to. A
        # group carries the type, the rounds and the ordering, so borrowing one
        # from another workout -- possibly another user's -- would give this
        # exercise a structure nobody performed. A plain foreign key on the id
        # alone accepts that; this one cannot. `exercise_group_id` is nullable,
        # and a partly-NULL composite key is simply not enforced, which is
        # exactly right for an exercise that belongs to no group.
        sa.ForeignKeyConstraint(
            ["exercise_group_id", "training_session_id"],
            ["exercise_groups.id", "exercise_groups.training_session_id"],
            name="fk_session_exercises_group_in_same_session",
            ondelete="RESTRICT",
        ),
        # The position is how a group's execution order is reconstructed, so
        # two exercises claiming the same one leaves it undecidable. Partial,
        # because "no group, no position" is the common case and NULLs must not
        # collide with each other.
        sa.Index(
            "uq_session_exercises_group_position",
            "exercise_group_id",
            "position_in_group",
            unique=True,
            postgresql_where=sa.text("exercise_group_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        sa.Index("ix_session_exercises_block", "training_session_id", "exercise_block_index"),
    )

    training_session_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("training_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NOT NULL on purpose: WS-7 resolves before anything is written, so an
    #: unresolved exercise never reaches a row (§16).
    exercise_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("exercises.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: The foreign key is the composite one in `__table_args__`: the group has
    #: to belong to this same session.
    exercise_group_id: Mapped[UUID | None] = mapped_column(sa.Uuid, nullable=True)
    position_in_group: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    exercise_block_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    #: Denormalized from the catalog at write time so a later catalog edit
    #: cannot retroactively change what a past workout says it was.
    activity_type: Mapped[ActivityType] = mapped_column(enum_column(ActivityType), nullable=False)
    raw_effort: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    normalized_rpe: Mapped[Decimal | None] = mapped_column(sa.Numeric(3, 1), nullable=True)
    effort_method: Mapped[EffortMethod | None] = mapped_column(
        enum_column(EffortMethod), nullable=True
    )
    effort_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    #: The current interaction's time (§7.3, Q32). Backdating is Sprint 4's.
    performed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    expected_version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class ExerciseSet(Base, SoftDeleteMixin):
    """One set. Every derived number carries the version that produced it."""

    __tablename__ = "exercise_sets"
    __table_args__ = (
        sa.UniqueConstraint("session_exercise_id", "set_index", name="uq_exercise_sets_index"),
        # Q52: a derived value without its version cannot be reproduced or
        # recomputed, which makes it worse than no value. The pairing is a
        # CHECK because a code review is a weaker place to enforce it.
        sa.CheckConstraint(
            "(volume_kg IS NULL) = (volume_metric_version IS NULL)",
            name="ck_exercise_sets_volume_versioned",
        ),
        sa.CheckConstraint(
            "(estimated_one_rm_kg IS NULL) = (one_rm_metric_version IS NULL)",
            name="ck_exercise_sets_one_rm_versioned",
        ),
        sa.CheckConstraint(
            "(pace_s_per_km IS NULL) = (pace_metric_version IS NULL)",
            name="ck_exercise_sets_pace_versioned",
        ),
        sa.CheckConstraint(
            "(speed_m_s IS NULL) = (speed_metric_version IS NULL)",
            name="ck_exercise_sets_speed_versioned",
        ),
    )

    session_exercise_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("session_exercises.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Never renumbered when a set is soft-deleted: the numbering is what the
    #: user was already shown, and rewriting it rewrites their history.
    set_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    set_type: Mapped[SetType] = mapped_column(
        enum_column(SetType), default=SetType.WORKING, nullable=False
    )

    repetitions: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    repetitions_provenance: Mapped[Provenance | None] = mapped_column(
        # Named explicitly: SQLAlchemy names an enum's CHECK after the enum,
        # and four Provenance columns in one table would collide.
        enum_column(Provenance, name="ck_exercise_sets_repetitions_provenance"),
        nullable=True,
    )
    #: SI throughout (D4). Numeric rather than float: 2.5 kg plates do not
    #: survive binary floating point unchanged, and a total that drifts by
    #: grams looks like a bug in the arithmetic to the person reading it.
    load_kg: Mapped[Decimal | None] = mapped_column(sa.Numeric(7, 3), nullable=True)
    load_mode: Mapped[LoadMode | None] = mapped_column(enum_column(LoadMode), nullable=True)
    load_provenance: Mapped[Provenance | None] = mapped_column(
        # Named explicitly: SQLAlchemy names an enum's CHECK after the enum,
        # and four Provenance columns in one table would collide.
        enum_column(Provenance, name="ck_exercise_sets_load_provenance"),
        nullable=True,
    )
    #: §19 keeps what the user reported separate from what it means: "60kg" on
    #: a machine is the reported load, and the effective one may differ.
    raw_load_text: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    distance_m: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 3), nullable=True)
    distance_provenance: Mapped[Provenance | None] = mapped_column(
        # Named explicitly: SQLAlchemy names an enum's CHECK after the enum,
        # and four Provenance columns in one table would collide.
        enum_column(Provenance, name="ck_exercise_sets_distance_provenance"),
        nullable=True,
    )
    duration_s: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 3), nullable=True)
    duration_provenance: Mapped[Provenance | None] = mapped_column(
        # Named explicitly: SQLAlchemy names an enum's CHECK after the enum,
        # and four Provenance columns in one table would collide.
        enum_column(Provenance, name="ck_exercise_sets_duration_provenance"),
        nullable=True,
    )

    raw_effort: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    normalized_rpe: Mapped[Decimal | None] = mapped_column(sa.Numeric(3, 1), nullable=True)
    effort_method: Mapped[EffortMethod | None] = mapped_column(
        enum_column(EffortMethod), nullable=True
    )
    effort_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    volume_kg: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 3), nullable=True)
    volume_metric_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    estimated_one_rm_kg: Mapped[Decimal | None] = mapped_column(sa.Numeric(7, 3), nullable=True)
    one_rm_metric_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    pace_s_per_km: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 3), nullable=True)
    pace_metric_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    speed_m_s: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 3), nullable=True)
    speed_metric_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    #: Per set, because the contract accepts it per set: "esteira" on one
    #: interval is a fact about that interval, and dropping it would lose
    #: something the user took the trouble to say.
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    expected_version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class ExerciseGroup(Base, SoftDeleteMixin):
    """A superset, triset, circuit or complex (Q51).

    A row rather than a naming convention on the block, because "I supersetted
    these two" is a fact about how the workout was performed and questions get
    asked of it later.
    """

    __tablename__ = "exercise_groups"
    __table_args__ = (
        sa.UniqueConstraint("training_session_id", "block_index", name="uq_exercise_groups_block"),
        # Redundant given the primary key, and required: it is what lets
        # `session_exercises` reference (group, session) as a pair, which is
        # how a group is kept from being borrowed by another workout.
        sa.UniqueConstraint("id", "training_session_id", name="uq_exercise_groups_id_session"),
    )

    training_session_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("training_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_type: Mapped[ExerciseGroupType] = mapped_column(
        enum_column(ExerciseGroupType), nullable=False
    )
    block_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    rounds: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class EntitySource(Base):
    """Which message a stored row exists because of (§26.2).

    Append-only and never updated: provenance that can be rewritten answers
    "why does the database say this" with whatever was most recently convenient.
    """

    __tablename__ = "entity_sources"
    __table_args__ = (
        # A row naming neither a message nor a batch records that something
        # came from somewhere, which is not provenance.
        sa.CheckConstraint(
            "message_id IS NOT NULL OR message_batch_id IS NOT NULL",
            name="ck_entity_sources_has_a_source",
        ),
        sa.Index("ix_entity_sources_entity", "entity_type", "entity_id"),
        sa.Index("ix_entity_sources_batch", "message_batch_id"),
    )

    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    message_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=True, index=True
    )
    message_batch_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("message_batches.id", ondelete="CASCADE"), nullable=True
    )
    source_role: Mapped[SourceRole] = mapped_column(enum_column(SourceRole), nullable=False)


class ExecutionTask(Base):
    """One task of one execution's plan, outside the checkpoint (Q118, Q128).

    §11.2's contract as columns. It exists so "what is this workflow doing" is
    a SQL question during the run and an audit afterwards, rather than
    something answerable only by decoding a LangGraph checkpoint -- which is
    coordination state, not a record of what happened.
    """

    __tablename__ = "execution_tasks"
    __table_args__ = (
        # Referenced by `pending_clarifications`' composite foreign key, which
        # is what stops a clarification from pointing at a task key that exists
        # under a different execution.
        sa.UniqueConstraint(
            "workflow_execution_id", "task_key", name="uq_execution_tasks_key_per_execution"
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'failed', 'skipped')) = (finished_at IS NOT NULL)",
            name="ck_execution_tasks_finished_when_terminal",
        ),
    )

    workflow_execution_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Stable within a plan: the plan's own identifier for the task, not a row
    #: id, so a re-planned redelivery collides with the row it already wrote.
    task_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    task_type: Mapped[TaskType] = mapped_column(
        enum_column(TaskType, name="ck_execution_tasks_task_type"), nullable=False
    )
    status: Mapped[TaskStatus] = mapped_column(
        enum_column(TaskStatus, name="ck_execution_tasks_status"),
        default=TaskStatus.PENDING,
        nullable=False,
    )
    result_visibility: Mapped[ResultVisibility] = mapped_column(
        enum_column(ResultVisibility, name="ck_execution_tasks_result_visibility"),
        default=ResultVisibility.USER_VISIBLE,
        nullable=False,
    )
    #: `[{"task_key": ..., "policy": ...}]`. Stored rather than derived because
    #: this table is read after the plan object is gone.
    depends_on: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=sa.text("'[]'::jsonb"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sa.text("'{}'::jsonb"), nullable=False
    )
    #: The facts the handler produced, kept for debugging and for the response
    #: guard's after-the-fact questions. Null until the task completes.
    result_facts: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class PendingClarification(Base):
    """A question the system is waiting on, as operational state (Q125).

    The checkpoint knows a workflow is suspended; it cannot be *searched* for
    "does this conversation have an open question", which is what every
    incoming message needs to ask. So the pause is mirrored here, in the same
    transaction, and this row -- not the checkpoint -- is what the resolver
    reads.

    It also carries where to resume from. A redelivered answer must fork from
    the checkpoint the interrupt was taken at rather than from wherever the
    previous attempt died, so `checkpoint_ns` and `checkpoint_id` are durable
    facts about the pause, not something reconstructed later.
    """

    __tablename__ = "pending_clarifications"
    __table_args__ = (
        sa.UniqueConstraint("clarification_id", name="uq_pending_clarifications_clarification_id"),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id", "task_key"],
            ["execution_tasks.workflow_execution_id", "execution_tasks.task_key"],
            name="fk_pending_clarifications_task_in_same_execution",
            ondelete="CASCADE",
        ),
        # At most one open question per conversation (D11). Two would make "the
        # answer" ambiguous with no deterministic way to disambiguate, and the
        # database is where that invariant belongs -- not in the resolver that
        # would otherwise have to guess.
        sa.Index(
            "uq_pending_clarifications_open_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=sa.text("status = 'waiting'"),
        ),
        sa.CheckConstraint(
            "(status = 'waiting') = (resolved_at IS NULL)",
            name="ck_pending_clarifications_resolved_when_closed",
        ),
        sa.Index("ix_pending_clarifications_expiry", "expires_at"),
    )

    #: What the `ClarificationSpec` carries, so the row and the checkpointed
    #: spec can be matched without decoding the checkpoint.
    clarification_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    workflow_execution_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    task_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    reason: Mapped[ClarificationReason] = mapped_column(
        enum_column(ClarificationReason, name="ck_pending_clarifications_reason"), nullable=False
    )
    status: Mapped[ClarificationStatus] = mapped_column(
        enum_column(ClarificationStatus, name="ck_pending_clarifications_status"),
        default=ClarificationStatus.WAITING,
        nullable=False,
    )
    #: The frozen `ClarificationSpec`, as sent. Stored whole so a question can
    #: be re-asked or audited without reconstructing it from the domain.
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    answer_message_batch_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("message_batches.id", ondelete="SET NULL"), nullable=True
    )
