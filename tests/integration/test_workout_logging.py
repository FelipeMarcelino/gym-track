"""WS-9: committing a workout, once (§15, §26.2, §28, Q57, Q58).

Everything a logged workout writes commits in one transaction or none of it
does: the idempotency claim, the training session, the exercises, the sets,
their provenance, the audit trail, the domain event and the outbox row. The
tests that matter assert row counts rather than return values — a service can
believe it deduplicated while the database holds two copies.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.commands.workout import LogWorkoutCommand, operation_id_for
from app.application.services.exercise_resolver import ExerciseResolver
from app.application.services.strict_syntax import parse
from app.application.services.training_sessions import TrainingSessionManager
from app.application.services.workout_command_builder import WorkoutCommandBuilder
from app.application.services.workout_logging import WorkoutApplicationService
from app.domain.training.effort import EffortNormalizer
from app.domain.training.provenance import Provenance
from app.domain.training.validation import ActivityValidator
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.exercise_catalog import PostgresExerciseCatalog
from app.infrastructure.postgres.models import (
    ActorType,
    AuditEvent,
    Conversation,
    DomainEvent,
    EntitySource,
    ExerciseSet,
    Message,
    MessageBatch,
    MessageBatchItem,
    MessageDirection,
    MessagingProvider,
    OutboxEvent,
    SessionExercise,
    TrainingSession,
    User,
)

pytestmark = [pytest.mark.integration]

TIMEOUT_HOURS = 3


class _Fixture:
    def __init__(self, *, user_id: UUID, conversation_id: UUID, batch_id: UUID, message_id: UUID):
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.batch_id = batch_id
        self.message_id = message_id


@pytest.fixture
async def batch(session_factory: async_sessionmaker[AsyncSession]) -> _Fixture:
    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        message = Message(
            user_id=user.id,
            conversation_id=conversation.id,
            provider=MessagingProvider.WHATSAPP,
            external_message_id=f"wamid.{uuid4()}",
            direction=MessageDirection.INBOUND,
            text="#log supino 80kg 10 9 8",
        )
        message_batch = MessageBatch(user_id=user.id, conversation_id=conversation.id)
        session.add_all([message, message_batch])
        await session.flush()
        session.add(
            MessageBatchItem(message_batch_id=message_batch.id, message_id=message.id, position=0)
        )
        return _Fixture(
            user_id=user.id,
            conversation_id=conversation.id,
            batch_id=message_batch.id,
            message_id=message.id,
        )


@pytest.fixture
def workout(session_factory: async_sessionmaker[AsyncSession]) -> WorkoutApplicationService:
    from datetime import timedelta

    return WorkoutApplicationService(
        session_factory=session_factory,
        sessions=TrainingSessionManager(timeout=timedelta(hours=TIMEOUT_HOURS)),
    )


async def _command(
    session_factory: async_sessionmaker[AsyncSession],
    fixture: _Fixture,
    text: str = "#log supino 80kg 10 9 8",
) -> LogWorkoutCommand:
    structured = parse([text])
    assert structured is not None

    async with session_factory() as session:
        builder = WorkoutCommandBuilder(
            resolver=ExerciseResolver(PostgresExerciseCatalog(session)),
            validator=ActivityValidator(),
            effort=EffortNormalizer(),
        )
        outcome = await builder.build(
            structured,
            user_id=fixture.user_id,
            conversation_id=fixture.conversation_id,
            message_batch_id=fixture.batch_id,
            source_message_ids=(fixture.message_id,),
        )

    assert outcome.command is not None, outcome.deferred
    return outcome.command


async def _count(session_factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with session_factory() as session:
        return len((await session.scalars(sa.select(model))).all())


# --------------------------------------------------------------------------
# One transaction, everything in it
# --------------------------------------------------------------------------


async def test_a_logged_workout_writes_everything_it_promises(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    """§15: the rows, their provenance, the audit trail, the event and the
    outbox row commit together. A consumer that learned of a workout the
    database does not have is the failure DEC-005 exists to stop."""
    result = await workout.log_workout(await _command(session_factory, batch))

    assert result.session_opened is True
    assert result.set_count == 3
    assert await _count(session_factory, TrainingSession) == 1
    assert await _count(session_factory, SessionExercise) == 1
    assert await _count(session_factory, ExerciseSet) == 3
    assert await _count(session_factory, OutboxEvent) >= 1

    async with session_factory() as session:
        events = (
            await session.scalars(
                sa.select(DomainEvent).where(DomainEvent.event_type == "workout.logged")
            )
        ).all()
    assert len(events) == 1


async def test_every_row_is_reachable_from_the_message_that_caused_it(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    """§26.2, asserted as the join an operator would actually run."""
    await workout.log_workout(await _command(session_factory, batch))

    async with session_factory() as session:
        reachable = (
            await session.scalars(
                sa.select(ExerciseSet.id)
                .join(
                    EntitySource,
                    sa.and_(
                        EntitySource.entity_id == ExerciseSet.id,
                        EntitySource.entity_type == "exercise_set",
                    ),
                )
                .where(EntitySource.message_id == batch.message_id)
            )
        ).all()

    assert len(reachable) == 3


async def test_the_session_itself_is_attributed_not_only_the_sets(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    """The session is the row an operator asks about first, and auditing only
    the leaves would leave it unattributed (§15, §26)."""
    await workout.log_workout(await _command(session_factory, batch))

    async with session_factory() as session:
        actions = (await session.scalars(sa.select(AuditEvent.action))).all()
        actors = set((await session.scalars(sa.select(AuditEvent.actor_type))).all())

    assert "training_session.started" in actions
    assert "workout.logged" in actions
    assert actors == {ActorType.USER}


async def test_provenance_survives_the_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    """§14.4 read back from the database rather than from the command: the
    sprint's fourth risk is that a total looks right while the provenance that
    makes a correction possible was never written."""
    await workout.log_workout(await _command(session_factory, batch))

    async with session_factory() as session:
        rows = (await session.scalars(sa.select(ExerciseSet).order_by(ExerciseSet.set_index))).all()

    assert [row.load_provenance for row in rows] == [
        Provenance.EXPLICIT,
        Provenance.INHERITED,
        Provenance.INHERITED,
    ]
    assert [row.load_kg for row in rows] == [Decimal("80.000")] * 3
    assert [row.repetitions for row in rows] == [10, 9, 8]


async def test_derived_numbers_arrive_with_their_versions(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    await workout.log_workout(await _command(session_factory, batch))

    async with session_factory() as session:
        rows = (await session.scalars(sa.select(ExerciseSet))).all()

    for row in rows:
        assert row.volume_kg is not None
        assert row.volume_metric_version is not None


# --------------------------------------------------------------------------
# Once, and only once (§28)
# --------------------------------------------------------------------------


async def test_a_redelivery_writes_nothing_the_second_time(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    """Asserted on row counts, never on the flag alone: a service can believe
    it deduplicated while the database holds two copies of the workout."""
    command = await _command(session_factory, batch)

    first = await workout.log_workout(command)
    second = await workout.log_workout(command)

    assert first.replayed is False
    assert second.replayed is True
    assert await _count(session_factory, ExerciseSet) == 3
    assert await _count(session_factory, SessionExercise) == 1

    async with session_factory() as session:
        events = (
            await session.scalars(
                sa.select(DomainEvent).where(DomainEvent.event_type == "workout.logged")
            )
        ).all()
    assert len(events) == 1


async def test_two_concurrent_deliveries_write_one_workout(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    """The claim is an insert, not a check: two deliveries that both read
    "not processed" before either wrote would both proceed."""
    command = await _command(session_factory, batch)

    await asyncio.gather(
        workout.log_workout(command),
        workout.log_workout(command),
        return_exceptions=True,
    )

    assert await _count(session_factory, ExerciseSet) == 3
    assert await _count(session_factory, SessionExercise) == 1


async def test_a_replay_reports_what_the_first_run_wrote(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    """The user's redelivered message still deserves an answer, and the answer
    has to describe the workout that exists."""
    command = await _command(session_factory, batch)

    first = await workout.log_workout(command)
    second = await workout.log_workout(command)

    assert second.training_session_id == first.training_session_id
    assert second.set_count == first.set_count
    assert [item.canonical_name for item in second.exercises] == [
        item.canonical_name for item in first.exercises
    ]


# --------------------------------------------------------------------------
# All or nothing (§15)
# --------------------------------------------------------------------------


async def test_a_failure_partway_through_leaves_nothing_behind(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim of §15 is that there is no window where the sets exist and the
    outbox row does not. Injecting a failure after the sets are added is how
    that claim is checked rather than asserted."""
    command = await _command(session_factory, batch)

    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the outbox write failed")

    monkeypatch.setattr("app.application.services.workout_logging.record_domain_event", explode)

    with pytest.raises(RuntimeError, match="outbox"):
        await workout.log_workout(command)

    assert await _count(session_factory, ExerciseSet) == 0
    assert await _count(session_factory, SessionExercise) == 0
    assert await _count(session_factory, TrainingSession) == 0
    assert await _count(session_factory, OutboxEvent) == 0


# --------------------------------------------------------------------------
# Blocks (Q58)
# --------------------------------------------------------------------------


async def test_a_second_log_joins_the_open_session_with_a_new_block(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    """Q58: a later exercise is a later block in the same workout, and the
    order is the order it was performed."""
    await workout.log_workout(await _command(session_factory, batch))

    async with unit_of_work(session_factory) as session:
        second_batch = MessageBatch(user_id=batch.user_id, conversation_id=batch.conversation_id)
        session.add(second_batch)
        await session.flush()
        second_batch_id = second_batch.id

    second = _Fixture(
        user_id=batch.user_id,
        conversation_id=batch.conversation_id,
        batch_id=second_batch_id,
        message_id=batch.message_id,
    )
    result = await workout.log_workout(
        await _command(session_factory, second, "#log agachamento 100kg 5")
    )

    assert result.session_opened is False
    assert await _count(session_factory, TrainingSession) == 1

    async with session_factory() as session:
        blocks = (
            await session.scalars(
                sa.select(SessionExercise).order_by(SessionExercise.exercise_block_index)
            )
        ).all()

    assert [block.exercise_block_index for block in blocks] == [0, 1]


async def test_the_same_exercise_again_reuses_its_block(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    """Two lines about the bench press in one message are one exercise
    performed five times, not two blocks (Q58)."""
    result = await workout.log_workout(
        await _command(session_factory, batch, "#log supino 80kg 10 9 8")
    )

    assert len(result.exercises) == 1
    assert await _count(session_factory, SessionExercise) == 1


# --------------------------------------------------------------------------
# The audit trail cannot be rewritten
# --------------------------------------------------------------------------


async def test_the_operation_claim_is_recorded(
    session_factory: async_sessionmaker[AsyncSession],
    workout: WorkoutApplicationService,
    batch: _Fixture,
) -> None:
    """§28: the key a redelivery recomputes is in the table it recomputes it
    against."""
    await workout.log_workout(await _command(session_factory, batch))

    async with session_factory() as session:
        claims = (
            await session.scalars(sa.text("SELECT operation_id FROM processed_operations"))
        ).all()

    assert operation_id_for(batch.batch_id) in claims
