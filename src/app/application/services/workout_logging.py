"""Committing a logged workout, once (§15, §26.2, §28, Q57, Q58).

One transaction holds everything: the idempotency claim, the training session,
the exercises and their sets, the provenance rows, the audit trail, the domain
event and the outbox row. There is no window in which the sets exist and the
outbox row does not — that is DEC-005's whole claim, and WS-11's
failure-injection test exists to check it rather than take this docstring's
word for it.

The claim is written *first*, as an insert. A check-then-act would let two
concurrent deliveries both read "not processed" and both proceed; letting the
unique index decide means the loser finds out by failing to claim, and reads
back what the winner wrote.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.commands.workout import ActivityCommand, LogWorkoutCommand, SetCommand
from app.application.services.training_sessions import Clock, TrainingSessionManager, utc_now
from app.domain.events import DomainEventEnvelope
from app.domain.training.metrics import (
    ONE_RM_VERSION,
    PACE_VERSION,
    SPEED_VERSION,
    VOLUME_VERSION,
    DerivedMetric,
)
from app.domain.training.provenance import SourceRole
from app.domain.training.workout_log import LoggedExercise, WorkoutLoggedResult
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    ActorType,
    AuditEvent,
    EntitySource,
    ExerciseGroup,
    ExerciseSet,
    ProcessedOperation,
    SessionExercise,
    TrainingSession,
)
from app.infrastructure.postgres.outbox import record_domain_event
from app.infrastructure.rabbitmq.topology import Exchanges

logger = logging.getLogger(__name__)

OPERATION_TYPE = "log_workout"
WORKOUT_LOGGED_EVENT = "workout.logged"
WORKOUT_LOGGED_AUDIT = "workout.logged"
GROUP_CREATED_AUDIT = "exercise_group.created"

#: Which column a derived metric lands in, and which column records the version
#: that produced it. Q52's pairing is a CHECK in the database; this is the map
#: that keeps the service from ever attempting the half of it that fails.
_METRIC_COLUMNS: dict[str, tuple[str, str, str]] = {
    VOLUME_VERSION: ("volume_kg", "volume_metric_version", "volume"),
    ONE_RM_VERSION: ("estimated_one_rm_kg", "one_rm_metric_version", "estimated_one_rm"),
    PACE_VERSION: ("pace_s_per_km", "pace_metric_version", "pace"),
    SPEED_VERSION: ("speed_m_s", "speed_metric_version", "speed"),
}


class WorkoutApplicationService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        sessions: TrainingSessionManager,
        clock: Clock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._sessions = sessions
        self._clock = clock

    async def log_workout(self, command: LogWorkoutCommand) -> WorkoutLoggedResult:
        """Write the workout, or report that it was already written (§28)."""
        async with unit_of_work(self._session_factory) as session:
            if not await self._claim(session, command):
                return await self._replay(session, command)

            training_session, opened = await self._sessions.start_or_resume(
                session, user_id=command.user_id, conversation_id=command.conversation_id
            )
            await self._sessions.touch(session, training_session)

            groups = await self._write_groups(session, command, training_session)
            logged = [
                await self._write_activity(session, command, training_session, activity, groups)
                for activity in command.activities
            ]

            await self._announce(session, command, training_session, logged)

        return WorkoutLoggedResult(
            training_session_id=training_session.id,
            session_opened=opened,
            exercises=tuple(logged),
        )

    # -- idempotency -------------------------------------------------------

    async def _claim(self, session: AsyncSession, command: LogWorkoutCommand) -> bool:
        """Claim this operation, or report that somebody already has.

        An insert rather than a read: two concurrent deliveries would both pass
        a check, and only one can win a unique index.
        """
        claimed = await session.scalar(
            insert(ProcessedOperation)
            .values(
                operation_id=command.operation_id,
                operation_type=OPERATION_TYPE,
                user_id=command.user_id,
            )
            .on_conflict_do_nothing(index_elements=["operation_id"])
            .returning(ProcessedOperation.id)
        )
        return claimed is not None

    async def _replay(
        self, session: AsyncSession, command: LogWorkoutCommand
    ) -> WorkoutLoggedResult:
        """Describe what the first run wrote, without writing anything.

        The user's redelivered message still deserves an answer, and the answer
        has to describe the workout that exists rather than the one this
        delivery would have written.
        """
        logger.info(
            "log workout replayed",
            extra={"operation_id": command.operation_id},
        )
        blocks = (
            await session.scalars(
                sa.select(SessionExercise)
                .join(
                    EntitySource,
                    sa.and_(
                        EntitySource.entity_id == SessionExercise.id,
                        EntitySource.entity_type == "session_exercise",
                    ),
                )
                .where(EntitySource.message_batch_id == command.message_batch_id)
                # One row per source message means several per block; the
                # blocks are what is being counted, not the provenance.
                .distinct()
                .order_by(SessionExercise.exercise_block_index)
            )
        ).all()

        exercises: list[LoggedExercise] = []
        training_session_id: UUID | None = None
        for block in blocks:
            training_session_id = block.training_session_id
            count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(ExerciseSet)
                .where(
                    ExerciseSet.session_exercise_id == block.id,
                    ExerciseSet.deleted_at.is_(None),
                )
            )
            exercises.append(
                LoggedExercise(
                    session_exercise_id=block.id,
                    canonical_name=await self._canonical_name(session, block),
                    block_index=block.exercise_block_index,
                    set_count=int(count or 0),
                )
            )

        if training_session_id is None:  # pragma: no cover - a claim implies rows
            raise RuntimeError(
                f"operation {command.operation_id} was claimed but wrote nothing; "
                "the first run committed the claim without the workout"
            )

        return WorkoutLoggedResult(
            training_session_id=training_session_id,
            session_opened=False,
            exercises=tuple(exercises),
            replayed=True,
        )

    async def _canonical_name(self, session: AsyncSession, block: SessionExercise) -> str:
        from app.infrastructure.postgres.models import Exercise

        name = await session.scalar(
            sa.select(Exercise.canonical_name).where(Exercise.id == block.exercise_id)
        )
        return str(name)

    # -- writing -----------------------------------------------------------

    async def _write_groups(
        self,
        session: AsyncSession,
        command: LogWorkoutCommand,
        training_session: TrainingSession,
    ) -> dict[str, ExerciseGroup]:
        groups: dict[str, ExerciseGroup] = {}
        for index, group in enumerate(command.groups):
            row = ExerciseGroup(
                training_session_id=training_session.id,
                group_type=group.group_type,
                block_index=await self._next_group_index(session, training_session, index),
                rounds=group.rounds,
            )
            session.add(row)
            await session.flush()
            groups[group.ref] = row
            self._audit(
                session,
                command,
                action=GROUP_CREATED_AUDIT,
                entity_type="exercise_group",
                entity_id=row.id,
            )
        return groups

    async def _next_group_index(
        self, session: AsyncSession, training_session: TrainingSession, offset: int
    ) -> int:
        highest = await session.scalar(
            sa.select(sa.func.max(ExerciseGroup.block_index)).where(
                ExerciseGroup.training_session_id == training_session.id
            )
        )
        return (int(highest) + 1 if highest is not None else 0) + offset

    async def _write_activity(
        self,
        session: AsyncSession,
        command: LogWorkoutCommand,
        training_session: TrainingSession,
        activity: ActivityCommand,
        groups: dict[str, ExerciseGroup],
    ) -> LoggedExercise:
        block, created = await self._block_for(session, training_session, activity, groups)
        first_index = await self._next_set_index(session, block)

        for offset, set_command in enumerate(activity.sets):
            row = self._set_row(block, set_command, index=first_index + offset)
            session.add(row)
            await session.flush()
            self._source(command, session, entity_type="exercise_set", entity_id=row.id)
            self._audit(
                session,
                command,
                action=WORKOUT_LOGGED_AUDIT,
                entity_type="exercise_set",
                entity_id=row.id,
            )

        if created:
            self._source(command, session, entity_type="session_exercise", entity_id=block.id)
            self._audit(
                session,
                command,
                action=WORKOUT_LOGGED_AUDIT,
                entity_type="session_exercise",
                entity_id=block.id,
            )

        return LoggedExercise(
            session_exercise_id=block.id,
            canonical_name=activity.canonical_name,
            block_index=block.exercise_block_index,
            set_count=len(activity.sets),
        )

    async def _block_for(
        self,
        session: AsyncSession,
        training_session: TrainingSession,
        activity: ActivityCommand,
        groups: dict[str, ExerciseGroup],
    ) -> tuple[SessionExercise, bool]:
        """The block these sets belong to, reusing the last one when it fits.

        Q58: consecutive sets of one exercise are that exercise performed
        again, and coming back to it after something else is a new block. The
        highest block is the only reuse candidate — anything earlier would
        reorder the workout.
        """
        highest = await session.scalar(
            sa.select(SessionExercise)
            .where(
                SessionExercise.training_session_id == training_session.id,
                SessionExercise.deleted_at.is_(None),
            )
            .order_by(SessionExercise.exercise_block_index.desc())
            .limit(1)
        )
        if highest is not None and highest.exercise_id == activity.exercise_id:
            return highest, False

        block = SessionExercise(
            training_session_id=training_session.id,
            exercise_id=activity.exercise_id,
            exercise_group_id=groups[activity.group_ref].id if activity.group_ref else None,
            position_in_group=(
                len([1 for key in groups if key == activity.group_ref]) - 1
                if activity.group_ref
                else None
            ),
            # Soft-deleted blocks still consume an index: renumbering would
            # rewrite an order the user was already shown.
            exercise_block_index=(highest.exercise_block_index + 1) if highest else 0,
            activity_type=activity.activity_type,
            raw_effort=activity.effort.raw if activity.effort else None,
            normalized_rpe=activity.effort.rpe if activity.effort else None,
            effort_method=activity.effort.method if activity.effort else None,
            effort_version=activity.effort.version if activity.effort else None,
            performed_at=self._clock(),
        )
        session.add(block)
        await session.flush()
        return block, True

    async def _next_set_index(self, session: AsyncSession, block: SessionExercise) -> int:
        highest = await session.scalar(
            sa.select(sa.func.max(ExerciseSet.set_index)).where(
                ExerciseSet.session_exercise_id == block.id
            )
        )
        return int(highest) + 1 if highest is not None else 0

    def _set_row(self, block: SessionExercise, command: SetCommand, *, index: int) -> ExerciseSet:
        row = ExerciseSet(
            session_exercise_id=block.id,
            set_index=index,
            set_type=command.set_type,
            repetitions=command.repetitions,
            repetitions_provenance=command.repetitions_provenance,
            load_kg=command.load_kg,
            load_mode=command.load_mode,
            load_provenance=command.load_provenance,
            raw_load_text=command.raw_load_text,
            distance_m=command.distance_m,
            distance_provenance=command.distance_provenance,
            duration_s=command.duration_s,
            duration_provenance=command.duration_provenance,
            raw_effort=command.effort.raw if command.effort else None,
            normalized_rpe=command.effort.rpe if command.effort else None,
            effort_method=command.effort.method if command.effort else None,
            effort_version=command.effort.version if command.effort else None,
            notes=command.notes,
        )
        self._apply_metrics(row, command.metrics)
        return row

    def _apply_metrics(self, row: ExerciseSet, metrics: Sequence[DerivedMetric]) -> None:
        """Each value with the version that produced it (Q52).

        Written through this map rather than field by field so a metric can
        never reach the database without its version -- the pairing is a CHECK,
        and a service that attempts half of it fails the whole transaction.
        """
        for metric in metrics:
            columns = _METRIC_COLUMNS.get(metric.version)
            if columns is None:  # pragma: no cover - a new metric needs a column
                raise LookupError(
                    f"derived metric {metric.name!r} has version {metric.version!r}, which no "
                    "column pair accepts; add the column before producing the metric"
                )
            value_column, version_column, _ = columns
            setattr(row, value_column, metric.value)
            setattr(row, version_column, metric.version)

    # -- provenance and announcement ---------------------------------------

    def _source(
        self,
        command: LogWorkoutCommand,
        session: AsyncSession,
        *,
        entity_type: str,
        entity_id: UUID,
    ) -> None:
        """One row per source message, each naming the batch too (§26.2).

        Per message rather than per batch alone: "which of my messages produced
        this set" is where a correction starts, and a batch-level answer cannot
        answer it. Each row carries both, so the batch question is still one
        query -- writing a separate batch-only row alongside them would record
        the same fact twice and make every count of it wrong.

        A batch with no messages still gets a row: provenance that points at
        nothing is what `ck_entity_sources_has_a_source` refuses.
        """
        if not command.source_message_ids:
            session.add(
                EntitySource(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    message_batch_id=command.message_batch_id,
                    source_role=SourceRole.CREATED_FROM,
                )
            )
            return

        for message_id in command.source_message_ids:
            session.add(
                EntitySource(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    message_id=message_id,
                    message_batch_id=command.message_batch_id,
                    source_role=SourceRole.CREATED_FROM,
                )
            )

    def _audit(
        self,
        session: AsyncSession,
        command: LogWorkoutCommand,
        *,
        action: str,
        entity_type: str,
        entity_id: UUID,
        occurred_at: datetime | None = None,
    ) -> None:
        session.add(
            AuditEvent(
                actor_type=ActorType.USER,
                actor_user_id=command.user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                metadata_={"operation_id": command.operation_id},
                occurred_at=occurred_at or self._clock(),
            )
        )

    async def _announce(
        self,
        session: AsyncSession,
        command: LogWorkoutCommand,
        training_session: TrainingSession,
        logged: Sequence[LoggedExercise],
    ) -> None:
        await record_domain_event(
            session,
            DomainEventEnvelope(
                event_type=WORKOUT_LOGGED_EVENT,
                aggregate_type="training_session",
                aggregate_id=training_session.id,
                user_id=command.user_id,
                payload={
                    "training_session_id": str(training_session.id),
                    "user_id": str(command.user_id),
                    "message_batch_id": str(command.message_batch_id),
                    "exercises": [exercise.canonical_name for exercise in logged],
                    "sets": sum(exercise.set_count for exercise in logged),
                },
            ),
            exchange=Exchanges.DOMAIN_EVENTS,
            routing_key=WORKOUT_LOGGED_EVENT,
        )
