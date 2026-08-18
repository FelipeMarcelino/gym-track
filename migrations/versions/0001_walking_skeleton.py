"""0001 — walking skeleton schema

The eleven tables the inbound-to-outbound path actually touches (§26). The rest
of the domain model arrives with the domain that writes it.

Every table carries a UUIDv7 primary key (decision D5) and creation/update
stamps. Enums are stored as checked strings rather than native PostgreSQL enum
types, so adding a value later is an ordinary migration.

Revision ID: 0001_walking_skeleton
Revises:
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_walking_skeleton"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "blocked", name="userstatus", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "conversations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "closed", name="conversationstatus", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_conversations_user_id"), "conversations", ["user_id"], unique=False)
    op.create_table(
        "domain_events",
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("causation_id", sa.String(length=64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_domain_events_aggregate",
        "domain_events",
        ["aggregate_type", "aggregate_id"],
        unique=False,
    )
    op.create_table(
        "user_identifiers",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("whatsapp", name="messagingprovider", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column("external_id_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("external_id_lookup_hmac", sa.LargeBinary(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "external_id_lookup_hmac", name="uq_user_identifiers_provider_lookup"
        ),
    )
    op.create_index(
        op.f("ix_user_identifiers_user_id"), "user_identifiers", ["user_id"], unique=False
    )
    op.create_table(
        "message_batches",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "flushed", name="messagebatchstatus", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "window_started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("flushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_message_batches_conversation_status",
        "message_batches",
        ["conversation_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_message_batches_user_id"), "message_batches", ["user_id"], unique=False
    )
    op.create_table(
        "messages",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("whatsapp", name="messagingprovider", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column("external_message_id", sa.String(length=255), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("inbound", "outbound", name="messagedirection", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column(
            "content_type",
            sa.Enum(
                "text",
                "audio",
                "image",
                "unsupported",
                name="messagecontenttype",
                native_enum=False,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("provider_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "external_message_id", name="uq_messages_provider_external_id"
        ),
    )
    op.create_index(
        "ix_messages_conversation_received",
        "messages",
        ["conversation_id", "received_at"],
        unique=False,
    )
    op.create_index(op.f("ix_messages_user_id"), "messages", ["user_id"], unique=False)
    op.create_table(
        "outbox_events",
        sa.Column("domain_event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "published", name="outboxstatus", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column("exchange", sa.String(length=128), nullable=False),
        sa.Column("routing_key", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["domain_event_id"], ["domain_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain_event_id", name="uq_outbox_events_domain_event"),
    )
    op.create_index(
        "ix_outbox_events_claimable", "outbox_events", ["status", "available_at"], unique=False
    )
    op.create_table(
        "message_batch_items",
        sa.Column("message_batch_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["message_batch_id"], ["message_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_batch_id", "message_id", name="uq_batch_items_batch_message"),
        sa.UniqueConstraint("message_batch_id", "position", name="uq_batch_items_batch_position"),
    )
    op.create_index(
        op.f("ix_message_batch_items_message_batch_id"),
        "message_batch_items",
        ["message_batch_id"],
        unique=False,
    )
    op.create_table(
        "workflow_executions",
        sa.Column("message_batch_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="workflowexecutionstatus",
                native_enum=False,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_batch_id"], ["message_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_batch_id", name="uq_workflow_executions_batch"),
    )
    op.create_index(
        op.f("ix_workflow_executions_user_id"), "workflow_executions", ["user_id"], unique=False
    )
    op.create_table(
        "outbound_messages",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_execution_id", sa.Uuid(), nullable=True),
        sa.Column("response_group_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "delivery_state",
            sa.Enum(
                "pending",
                "dispatching",
                "dispatched",
                "delivered",
                "failed",
                name="deliverystate",
                native_enum=False,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"], ["workflow_executions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "response_group_id", "sequence", name="uq_outbound_messages_group_sequence"
        ),
    )
    op.create_index(
        "ix_outbound_messages_state",
        "outbound_messages",
        ["delivery_state", "response_group_id", "sequence"],
        unique=False,
    )
    op.create_index(
        op.f("ix_outbound_messages_user_id"), "outbound_messages", ["user_id"], unique=False
    )
    op.create_table(
        "processed_operations",
        sa.Column("operation_id", sa.String(length=255), nullable=False),
        sa.Column("operation_type", sa.String(length=128), nullable=False),
        sa.Column("workflow_execution_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"], ["workflow_executions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", name="uq_processed_operations_operation_id"),
    )


def downgrade() -> None:
    op.drop_table("processed_operations")
    op.drop_index(op.f("ix_outbound_messages_user_id"), table_name="outbound_messages")
    op.drop_index("ix_outbound_messages_state", table_name="outbound_messages")
    op.drop_table("outbound_messages")
    op.drop_index(op.f("ix_workflow_executions_user_id"), table_name="workflow_executions")
    op.drop_table("workflow_executions")
    op.drop_index(op.f("ix_message_batch_items_message_batch_id"), table_name="message_batch_items")
    op.drop_table("message_batch_items")
    op.drop_index("ix_outbox_events_claimable", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index(op.f("ix_messages_user_id"), table_name="messages")
    op.drop_index("ix_messages_conversation_received", table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_message_batches_user_id"), table_name="message_batches")
    op.drop_index("ix_message_batches_conversation_status", table_name="message_batches")
    op.drop_table("message_batches")
    op.drop_index(op.f("ix_user_identifiers_user_id"), table_name="user_identifiers")
    op.drop_table("user_identifiers")
    op.drop_index("ix_domain_events_aggregate", table_name="domain_events")
    op.drop_table("domain_events")
    op.drop_index(op.f("ix_conversations_user_id"), table_name="conversations")
    op.drop_table("conversations")
    op.drop_table("users")
