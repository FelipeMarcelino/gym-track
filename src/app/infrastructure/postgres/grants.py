"""Who may write what (Q145).

Least privilege between infrastructure services, not just between agents. The
policy lives here rather than inside a migration so that it can be asserted by
tests and reviewed as code; migration 0002 is only what applies it.

Two invariants hold across the whole table and are enforced by tests:

* No service is granted DELETE on anything. Removal is soft (§26).
* No service is granted UPDATE on an append-only table: `domain_events` is
  history, and the database is what makes that true rather than a convention.
"""

from __future__ import annotations

from typing import Final

from app.config import ServiceName
from app.infrastructure.postgres.schemas import CHECKPOINT_SCHEMA

#: table -> privileges, per service. Anything absent is denied by omission.
SERVICE_GRANTS: Final[dict[ServiceName, dict[str, tuple[str, ...]]]] = {
    ServiceName.API: {
        "users": ("SELECT", "INSERT", "UPDATE"),
        "user_identifiers": ("SELECT", "INSERT"),
        "conversations": ("SELECT", "INSERT", "UPDATE"),
        "messages": ("SELECT", "INSERT"),
        "domain_events": ("SELECT", "INSERT"),
        "outbox_events": ("SELECT", "INSERT"),
    },
    ServiceName.MESSAGE_AGGREGATOR: {
        "users": ("SELECT",),
        "conversations": ("SELECT", "UPDATE"),
        "messages": ("SELECT",),
        "message_batches": ("SELECT", "INSERT", "UPDATE"),
        "message_batch_items": ("SELECT", "INSERT"),
        "domain_events": ("SELECT", "INSERT"),
        "outbox_events": ("SELECT", "INSERT"),
    },
    ServiceName.WORKFLOW_WORKER: {
        "users": ("SELECT",),
        "training_sessions": ("SELECT", "INSERT", "UPDATE"),
        "session_exercises": ("SELECT", "INSERT", "UPDATE"),
        "exercise_sets": ("SELECT", "INSERT", "UPDATE"),
        "exercise_groups": ("SELECT", "INSERT", "UPDATE"),
        # No UPDATE: provenance is history, and history that can be edited
        # answers "why does the database say this" with whatever was most
        # recently convenient (§26.2).
        "entity_sources": ("SELECT", "INSERT"),
        "audit_events": ("SELECT", "INSERT"),
        # The catalog is read-only to every process except for learned aliases,
        # which Sprint 3 writes when a user answers a clarification.
        "exercises": ("SELECT",),
        "exercise_aliases": ("SELECT", "INSERT"),
        "muscles": ("SELECT",),
        "exercise_muscles": ("SELECT",),
        "equipment": ("SELECT",),
        "exercise_equipment": ("SELECT",),
        "exercise_relations": ("SELECT",),
        "conversations": ("SELECT",),
        "messages": ("SELECT",),
        "message_batches": ("SELECT", "UPDATE"),
        "message_batch_items": ("SELECT",),
        "workflow_executions": ("SELECT", "INSERT", "UPDATE"),
        "processed_operations": ("SELECT", "INSERT"),
        "outbound_messages": ("SELECT", "INSERT"),
        "domain_events": ("SELECT", "INSERT"),
        "outbox_events": ("SELECT", "INSERT"),
    },
    ServiceName.OUTBOX_PUBLISHER: {
        "domain_events": ("SELECT",),
        "outbox_events": ("SELECT", "UPDATE"),
    },
    ServiceName.SESSION_EXPIRATION_WORKER: {
        "users": ("SELECT",),
        # No INSERT, deliberately: a bug that made the sweep *open* a session
        # is refused by PostgreSQL rather than discovered in a user's history.
        "training_sessions": ("SELECT", "UPDATE"),
        # It closes sessions, so it attributes them.
        "audit_events": ("SELECT", "INSERT"),
        "domain_events": ("SELECT", "INSERT"),
        "outbox_events": ("SELECT", "INSERT"),
    },
    ServiceName.DISPATCHER: {
        "users": ("SELECT",),
        # It resolves the recipient's plaintext identifier before every send;
        # without this grant a deployed dispatcher aborts every group.
        "user_identifiers": ("SELECT",),
        "conversations": ("SELECT",),
        "outbound_messages": ("SELECT", "UPDATE"),
        "domain_events": ("SELECT", "INSERT"),
        "outbox_events": ("SELECT", "INSERT"),
    },
}

#: schema -> privileges on every table in it, per service, for schemas outside
#: `public`. Only one exists: LangGraph's checkpoint store (ADR-015).
#:
#: This is the single place in the system where DELETE is granted, and the
#: invariant test above is right to say no *service table* grants it: §26's soft
#: deletion is about domain rows whose history is worth keeping. A checkpointer
#: that cannot delete its own superseded writes grows without bound, and a
#: checkpoint is rebuildable coordination state rather than history.
SCHEMA_GRANTS: Final[dict[ServiceName, dict[str, tuple[str, ...]]]] = {
    ServiceName.WORKFLOW_WORKER: {
        CHECKPOINT_SCHEMA: ("SELECT", "INSERT", "UPDATE", "DELETE"),
    },
}

ALL_TABLES: Final[tuple[str, ...]] = (
    "users",
    "training_sessions",
    "session_exercises",
    "exercise_sets",
    "exercise_groups",
    "entity_sources",
    "audit_events",
    "exercises",
    "exercise_aliases",
    "muscles",
    "exercise_muscles",
    "equipment",
    "exercise_equipment",
    "exercise_relations",
    "user_identifiers",
    "conversations",
    "messages",
    "message_batches",
    "message_batch_items",
    "workflow_executions",
    "processed_operations",
    "outbound_messages",
    "domain_events",
    "outbox_events",
)
