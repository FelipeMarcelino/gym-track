"""Opening, resuming and closing training sessions (§18, Q31, Q32).

A session starts when logging occurs and no valid one is open, and ends after
inactivity. Two things end it: the next workout input noticing on its way
through (the lazy path), and the background sweep. Both use the same policy
from `app.domain.training.sessions`, and both write the same audit row — a
session that closed itself with nobody attributed is exactly the gap
`audit_events` exists to prevent.

§18 is explicit that PostgreSQL's `last_activity_at` is authoritative. A Redis
hint may narrow where the sweep looks; it never decides.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.session_hints import SessionHintStore
from app.domain.events import DomainEventEnvelope
from app.domain.identifiers import new_uuid7
from app.domain.training.sessions import SessionCloseReason, is_expired
from app.infrastructure.postgres.models import (
    ActorType,
    AuditEvent,
    TrainingSession,
    TrainingSessionStatus,
)
from app.infrastructure.postgres.outbox import record_domain_event
from app.infrastructure.rabbitmq.topology import Exchanges

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

SESSION_STARTED = "training_session.started"
SESSION_CLOSED = "training_session.closed"
SESSION_FINISHED_EVENT = "training_session.finished"


def utc_now() -> datetime:
    return datetime.now(UTC)


class TrainingSessionManager:
    def __init__(
        self,
        *,
        timeout: timedelta,
        clock: Clock = utc_now,
        hints: SessionHintStore | None = None,
    ) -> None:
        self._timeout = timeout
        #: Written wherever activity is observed. The sweep reads it to decide
        #: where to look first; nothing reads it to decide anything else.
        self._hints = hints
        #: Injectable so a test can move time without sleeping, and so the
        #: worker and the lazy path can be shown to agree at the same instant.
        self._clock = clock

    async def active_session(self, session: AsyncSession, user_id: UUID) -> TrainingSession | None:
        """The user's open session, or None once lazy expiry has been applied.

        Q31: the next workout input is a fallback for the sweep, so this is the
        path that must not return a session the timeout already ended.
        """
        current = await self._locked_active_session(session, user_id)
        if current is None:
            return None

        if is_expired(
            last_activity_at=current.last_activity_at,
            now=self._clock(),
            timeout=self._timeout,
        ):
            await self.close(
                session,
                current,
                reason=SessionCloseReason.LAZY,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
            )
            return None

        return current

    async def start_or_resume(
        self, session: AsyncSession, *, user_id: UUID, conversation_id: UUID
    ) -> tuple[TrainingSession, bool]:
        """Returns (session, opened). Closes an expired one on the way through."""
        current = await self.active_session(session, user_id)
        if current is not None:
            return current, False

        now = self._clock()

        # The lazy close above is still pending in the unit of work, and the
        # partial index is enforced against what the database can see. Without
        # this flush the expired row still counts as active and the insert below
        # conflicts with a session that was just closed.
        await session.flush()

        # `ON CONFLICT DO NOTHING` rather than insert-and-catch: a failed flush
        # leaves the session needing a rollback even inside a savepoint, so the
        # recovery path would have to unwind more than it fixed. Letting
        # PostgreSQL decline the insert keeps the transaction usable, and it is
        # the same shape WS-7 of Sprint 1 uses for duplicate messages.
        statement = (
            insert(TrainingSession)
            .values(
                id=new_uuid7(),
                user_id=user_id,
                conversation_id=conversation_id,
                status=TrainingSessionStatus.ACTIVE,
                started_at=now,
                last_activity_at=now,
                expected_version=1,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id"],
                index_where=sa.text("status = 'active' AND deleted_at IS NULL"),
            )
            .returning(TrainingSession.id)
        )
        inserted_id = await session.scalar(statement)

        if inserted_id is None:
            # Another delivery opened this user's session between the read
            # above and the insert. The winner is the session.
            winner = await self._locked_active_session(session, user_id)
            if winner is None:  # pragma: no cover - the conflict implies a winner
                raise RuntimeError("the insert conflicted but no active session exists")
            return winner, False

        training_session = await session.get(TrainingSession, inserted_id)
        if training_session is None:  # pragma: no cover - it was just inserted
            raise RuntimeError(f"training session {inserted_id} vanished after insert")

        await self._note_activity(user_id)
        await self._audit(
            session,
            action=SESSION_STARTED,
            training_session=training_session,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
        )
        return training_session, True

    async def touch(self, session: AsyncSession, training_session: TrainingSession) -> None:
        """Refresh the column §18 makes authoritative, and hint at it."""
        training_session.last_activity_at = self._clock()
        await self._note_activity(training_session.user_id)

    async def _note_activity(self, user_id: UUID) -> None:
        """Tell the hint store, and never fail the workout over it.

        Redis is non-authoritative here (§10): losing a hint costs the sweep a
        wider scan. Losing the user's log because a performance hint could not
        be stored would trade the record for the index.
        """
        if self._hints is None:
            return
        try:
            await self._hints.note_activity(user_id)
        except Exception:
            logger.warning("could not write the expiry hint", exc_info=True)

    async def close(
        self,
        session: AsyncSession,
        training_session: TrainingSession,
        *,
        reason: SessionCloseReason,
        actor_type: ActorType,
        actor_user_id: UUID | None,
    ) -> None:
        """Close a session, record who ended it, and announce that it ended."""
        now = self._clock()
        training_session.status = TrainingSessionStatus.CLOSED
        training_session.finished_at = now
        training_session.closed_by = reason.value

        await self._audit(
            session,
            action=SESSION_CLOSED,
            training_session=training_session,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            metadata={"closed_by": reason.value},
        )
        await record_domain_event(
            session,
            DomainEventEnvelope(
                event_type=SESSION_FINISHED_EVENT,
                aggregate_type="training_session",
                aggregate_id=training_session.id,
                user_id=training_session.user_id,
                payload={
                    "training_session_id": str(training_session.id),
                    "user_id": str(training_session.user_id),
                    "closed_by": reason.value,
                },
            ),
            exchange=Exchanges.DOMAIN_EVENTS,
            routing_key=SESSION_FINISHED_EVENT,
        )
        logger.info(
            "training session closed",
            extra={
                "training_session_id": str(training_session.id),
                "closed_by": reason.value,
            },
        )

    async def close_expired(
        self,
        session: AsyncSession,
        *,
        limit: int = 100,
        candidates: Sequence[UUID] | None = None,
    ) -> list[UUID]:
        """Close every session whose inactivity has run out. Returns their ids.

        `candidates` is a *hint*, nothing more: §18 puts the authority in
        `last_activity_at`, so every candidate is re-checked against it here.
        A hint that outlived the activity it described closes nothing.
        """
        now = self._clock()
        statement = (
            sa.select(TrainingSession)
            .where(
                TrainingSession.status == TrainingSessionStatus.ACTIVE,
                TrainingSession.deleted_at.is_(None),
            )
            .order_by(TrainingSession.last_activity_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        # `is not None` rather than truthiness: an empty sequence means "these
        # users, of which there are none", and reading it as "no filter" would
        # turn an empty hint scan into a full sweep nobody asked for.
        if candidates is not None:
            statement = statement.where(TrainingSession.user_id.in_(list(candidates)))

        closed: list[UUID] = []
        for training_session in (await session.scalars(statement)).all():
            if not is_expired(
                last_activity_at=training_session.last_activity_at,
                now=now,
                timeout=self._timeout,
            ):
                continue
            await self.close(
                session,
                training_session,
                reason=SessionCloseReason.WORKER,
                # Nobody was there. Recording a user would attribute an action
                # to someone who did not take it.
                actor_type=ActorType.SYSTEM,
                actor_user_id=None,
            )
            closed.append(training_session.id)

        return closed

    async def _locked_active_session(
        self, session: AsyncSession, user_id: UUID
    ) -> TrainingSession | None:
        """The open session, locked so two deliveries cannot both act on it."""
        found: TrainingSession | None = await session.scalar(
            sa.select(TrainingSession)
            .where(
                TrainingSession.user_id == user_id,
                TrainingSession.status == TrainingSessionStatus.ACTIVE,
                TrainingSession.deleted_at.is_(None),
            )
            .order_by(TrainingSession.last_activity_at.desc())
            .limit(1)
            .with_for_update()
        )
        return found

    async def _audit(
        self,
        session: AsyncSession,
        *,
        action: str,
        training_session: TrainingSession,
        actor_type: ActorType,
        actor_user_id: UUID | None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        session.add(
            AuditEvent(
                actor_type=actor_type,
                actor_user_id=actor_user_id,
                action=action,
                entity_type="training_session",
                entity_id=training_session.id,
                metadata_=metadata or {},
                occurred_at=self._clock(),
            )
        )
