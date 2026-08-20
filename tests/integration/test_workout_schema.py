"""WS-6: the tables a logged workout lands in (§14.4, §19, §26.2, Q51, Q52, Q58).

Everything here is asserted against a real PostgreSQL, because the point of the
workstream is what the *database* refuses. A CHECK constraint that only Python
knows about is a comment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.training.activities import ActivityType, LoadMode, SetType
from app.domain.training.metrics import VOLUME_VERSION
from app.domain.training.provenance import ExerciseGroupType, Provenance, SourceRole
from app.infrastructure.postgres.base import select_active
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    Conversation,
    EntitySource,
    Exercise,
    ExerciseGroup,
    ExerciseSet,
    Message,
    MessageDirection,
    MessagingProvider,
    SessionExercise,
    TrainingSession,
    User,
)

pytestmark = [pytest.mark.integration]


class _Workout:
    """The rows every test here needs before it can say anything interesting."""

    def __init__(self, *, user_id: UUID, session_id: UUID, message_id: UUID) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.message_id = message_id


@pytest.fixture
async def workout(session_factory: async_sessionmaker[AsyncSession]) -> _Workout:
    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        training_session = TrainingSession(user_id=user.id, conversation_id=conversation.id)
        message = Message(
            user_id=user.id,
            conversation_id=conversation.id,
            provider=MessagingProvider.WHATSAPP,
            external_message_id="wamid.workout",
            direction=MessageDirection.INBOUND,
            text="supino 3x10 60kg",
        )
        session.add_all([training_session, message])
        await session.flush()
        return _Workout(user_id=user.id, session_id=training_session.id, message_id=message.id)


async def _exercise_id(session_factory: async_sessionmaker[AsyncSession], slug: str) -> UUID:
    async with session_factory() as session:
        return (await session.scalars(sa.select(Exercise.id).where(Exercise.slug == slug))).one()


def _set(session_exercise_id: UUID, index: int, **kwargs: object) -> ExerciseSet:
    return ExerciseSet(session_exercise_id=session_exercise_id, set_index=index, **kwargs)


async def _block(
    session: AsyncSession, workout: _Workout, exercise_id: UUID, block_index: int
) -> SessionExercise:
    block = SessionExercise(
        training_session_id=workout.session_id,
        exercise_id=exercise_id,
        exercise_block_index=block_index,
        activity_type=ActivityType.STRENGTH,
        performed_at=datetime.now(UTC),
    )
    session.add(block)
    await session.flush()
    return block


# --------------------------------------------------------------------------
# Blocks and sets (Q58)
# --------------------------------------------------------------------------


async def test_consecutive_sets_of_one_exercise_share_a_block(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """Three sets of the bench press are one exercise performed three times,
    not three exercises."""
    bench = await _exercise_id(session_factory, "supino-reto")

    async with unit_of_work(session_factory) as session:
        block = await _block(session, workout, bench, 0)
        for index in range(3):
            session.add(_set(block.id, index, repetitions=10))

    async with session_factory() as session:
        blocks = (await session.scalars(sa.select(SessionExercise))).all()
        indexes = (
            await session.scalars(sa.select(ExerciseSet.set_index).order_by(ExerciseSet.set_index))
        ).all()

    assert len(blocks) == 1
    assert list(indexes) == [0, 1, 2]


async def test_a_b_a_keeps_the_order_it_was_performed_in(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """Q58: coming back to an exercise later is a second block, and the block
    indexes preserve what happened rather than grouping by exercise. A history
    that reordered the workout would answer "what did I do first" wrongly."""
    bench = await _exercise_id(session_factory, "supino-reto")
    squat = await _exercise_id(session_factory, "agachamento-livre")

    async with unit_of_work(session_factory) as session:
        await _block(session, workout, bench, 0)
        await _block(session, workout, squat, 1)
        await _block(session, workout, bench, 2)

    async with session_factory() as session:
        rows = (
            await session.scalars(
                sa.select(SessionExercise).order_by(SessionExercise.exercise_block_index)
            )
        ).all()

    assert [row.exercise_id for row in rows] == [bench, squat, bench]
    assert [row.exercise_block_index for row in rows] == [0, 1, 2]


async def test_a_block_index_cannot_repeat_within_a_session(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """Two blocks claiming the same position is an order that cannot be read
    back, so the database refuses it rather than picking one."""
    bench = await _exercise_id(session_factory, "supino-reto")
    squat = await _exercise_id(session_factory, "agachamento-livre")

    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            await _block(session, workout, bench, 0)
            await _block(session, workout, squat, 0)


async def test_a_soft_deleted_set_keeps_the_numbering_of_the_others(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """Renumbering on delete would rewrite what the user already saw us confirm;
    the index stays, and the default read path stops returning the row."""
    bench = await _exercise_id(session_factory, "supino-reto")

    async with unit_of_work(session_factory) as session:
        block = await _block(session, workout, bench, 0)
        for index in range(3):
            session.add(_set(block.id, index, repetitions=10))

    async with unit_of_work(session_factory) as session:
        middle = (
            await session.scalars(sa.select(ExerciseSet).where(ExerciseSet.set_index == 1))
        ).one()
        middle.deleted_at = datetime.now(UTC)

    async with session_factory() as session:
        remaining = (
            await session.scalars(select_active(ExerciseSet).order_by(ExerciseSet.set_index))
        ).all()
        every_row = (await session.scalars(sa.select(ExerciseSet))).all()

    assert [row.set_index for row in remaining] == [0, 2]
    assert len(every_row) == 3, "soft delete keeps the row"


# --------------------------------------------------------------------------
# Provenance (§14.4) and derived metrics (Q52)
# --------------------------------------------------------------------------


async def test_a_set_records_which_of_its_values_were_stated(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """§14.4: "3x10 60kg then 2 more sets" states the reps and inherits the
    load. Storing both as though the user said them loses the difference that
    a later correction depends on."""
    bench = await _exercise_id(session_factory, "supino-reto")

    async with unit_of_work(session_factory) as session:
        block = await _block(session, workout, bench, 0)
        session.add(
            _set(
                block.id,
                0,
                repetitions=10,
                repetitions_provenance=Provenance.EXPLICIT,
                load_kg=Decimal("60.000"),
                load_mode=LoadMode.TOTAL,
                load_provenance=Provenance.INHERITED,
                raw_load_text="60kg",
                set_type=SetType.WORKING,
            )
        )

    async with session_factory() as session:
        stored = (await session.scalars(sa.select(ExerciseSet))).one()

    assert stored.repetitions_provenance is Provenance.EXPLICIT
    assert stored.load_provenance is Provenance.INHERITED
    assert stored.raw_load_text == "60kg"


async def test_a_derived_value_cannot_be_stored_without_its_version(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """Q52: a number nobody can reproduce is worse than no number. The pairing
    is a CHECK because a review is a weaker place to enforce it."""
    bench = await _exercise_id(session_factory, "supino-reto")

    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            block = await _block(session, workout, bench, 0)
            session.add(_set(block.id, 0, repetitions=10, volume_kg=Decimal("600.000")))


async def test_a_derived_value_with_its_version_is_accepted(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    bench = await _exercise_id(session_factory, "supino-reto")

    async with unit_of_work(session_factory) as session:
        block = await _block(session, workout, bench, 0)
        session.add(
            _set(
                block.id,
                0,
                repetitions=10,
                load_kg=Decimal("60.000"),
                volume_kg=Decimal("600.000"),
                volume_metric_version=VOLUME_VERSION,
            )
        )

    async with session_factory() as session:
        stored = (await session.scalars(sa.select(ExerciseSet))).one()

    assert stored.volume_metric_version == VOLUME_VERSION


# --------------------------------------------------------------------------
# Groups (Q51)
# --------------------------------------------------------------------------


async def test_a_superset_links_its_exercises_by_position(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """Q51: a superset is a fact about how two exercises were performed, so it
    is a row rather than a naming convention on the block."""
    bench = await _exercise_id(session_factory, "supino-reto")
    row_exercise = await _exercise_id(session_factory, "remada-curvada")

    async with unit_of_work(session_factory) as session:
        group = ExerciseGroup(
            training_session_id=workout.session_id,
            group_type=ExerciseGroupType.SUPERSET,
            block_index=0,
            rounds=3,
        )
        session.add(group)
        await session.flush()
        for position, exercise_id in enumerate((bench, row_exercise)):
            block = await _block(session, workout, exercise_id, position)
            block.exercise_group_id = group.id
            block.position_in_group = position

    async with session_factory() as session:
        stored = (
            await session.scalars(
                sa.select(SessionExercise).order_by(SessionExercise.position_in_group)
            )
        ).all()

    assert [row.position_in_group for row in stored] == [0, 1]
    assert len({row.exercise_group_id for row in stored}) == 1


async def test_a_block_cannot_join_a_group_from_another_session(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """A group carries the type, the rounds and the ordering. Borrowing one
    from a different workout — possibly a different user's — would give this
    exercise a structure nobody performed, so the reference is scoped to the
    session rather than to the group id alone."""
    bench = await _exercise_id(session_factory, "supino-reto")

    async with unit_of_work(session_factory) as session:
        other_user = User()
        session.add(other_user)
        await session.flush()
        other_session = TrainingSession(user_id=other_user.id)
        session.add(other_session)
        await session.flush()
        foreign_group = ExerciseGroup(
            training_session_id=other_session.id,
            group_type=ExerciseGroupType.SUPERSET,
            block_index=0,
        )
        session.add(foreign_group)
        await session.flush()
        foreign_group_id = foreign_group.id

    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            block = await _block(session, workout, bench, 0)
            block.exercise_group_id = foreign_group_id
            block.position_in_group = 0


async def test_two_exercises_cannot_hold_the_same_place_in_a_group(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """The position is how the group's execution order is reconstructed, and
    two exercises claiming the same one leaves it undecidable."""
    bench = await _exercise_id(session_factory, "supino-reto")
    row_exercise = await _exercise_id(session_factory, "remada-curvada")

    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            group = ExerciseGroup(
                training_session_id=workout.session_id,
                group_type=ExerciseGroupType.SUPERSET,
                block_index=0,
            )
            session.add(group)
            await session.flush()
            for block_index, exercise_id in enumerate((bench, row_exercise)):
                block = await _block(session, workout, exercise_id, block_index)
                block.exercise_group_id = group.id
                block.position_in_group = 0


async def test_ungrouped_blocks_do_not_collide_with_each_other(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """Most exercises belong to no group. The position uniqueness must not turn
    "no group, no position" into a value two rows can conflict over."""
    bench = await _exercise_id(session_factory, "supino-reto")
    squat = await _exercise_id(session_factory, "agachamento-livre")

    async with unit_of_work(session_factory) as session:
        await _block(session, workout, bench, 0)
        await _block(session, workout, squat, 1)

    async with session_factory() as session:
        blocks = (await session.scalars(sa.select(SessionExercise))).all()

    assert len(blocks) == 2


# --------------------------------------------------------------------------
# Provenance to the message (§26.2)
# --------------------------------------------------------------------------


async def test_every_set_is_reachable_from_the_message_that_created_it(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """§26.2 asks the question in the other direction too: given a message,
    what did we write because of it. Asserted as a join, because that is how it
    will actually be answered."""
    bench = await _exercise_id(session_factory, "supino-reto")

    async with unit_of_work(session_factory) as session:
        block = await _block(session, workout, bench, 0)
        first = _set(block.id, 0, repetitions=10)
        session.add(first)
        await session.flush()
        session.add(
            EntitySource(
                entity_type="exercise_set",
                entity_id=first.id,
                message_id=workout.message_id,
                source_role=SourceRole.CREATED_FROM,
            )
        )

    async with session_factory() as session:
        found = (
            await session.scalars(
                sa.select(ExerciseSet.id)
                .join(
                    EntitySource,
                    sa.and_(
                        EntitySource.entity_id == ExerciseSet.id,
                        EntitySource.entity_type == "exercise_set",
                    ),
                )
                .where(EntitySource.message_id == workout.message_id)
            )
        ).all()

    assert len(found) == 1


async def test_a_source_row_must_point_at_something(
    session_factory: async_sessionmaker[AsyncSession], workout: _Workout
) -> None:
    """A provenance row naming neither a message nor a batch records that
    something came from somewhere, which is not provenance."""
    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(
                EntitySource(
                    entity_type="exercise_set",
                    entity_id=workout.session_id,
                    source_role=SourceRole.CREATED_FROM,
                )
            )
