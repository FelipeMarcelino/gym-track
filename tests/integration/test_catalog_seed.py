"""WS-1 against real PostgreSQL: the seed converges and the constraints bite."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ApplicationSettings
from app.domain.exercises.catalog import AliasSource, MuscleRole
from app.domain.exercises.catalog_data import CATALOG_SEED, SEED_RELATIONS
from app.domain.training.activities import ActivityType, LoadMode
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    Equipment,
    Exercise,
    ExerciseAlias,
    ExerciseEquipment,
    ExerciseMuscle,
    ExerciseRelation,
    Muscle,
    User,
)
from app.infrastructure.postgres.seeding import normalize_for_match, seed_catalog_sync
from tests.conftest import alembic_config

pytestmark = [pytest.mark.integration]


async def _count(session_factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with session_factory() as session:
        return int(await session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


async def _reseed(migrated_database: ApplicationSettings) -> Any:
    """Run the seed the way `make seed` does, against the live database."""
    from sqlalchemy import create_engine

    engine = create_engine(migrated_database.postgres.admin_dsn().replace("+asyncpg", "+psycopg"))
    try:
        with engine.begin() as connection:
            return seed_catalog_sync(connection)
    finally:
        engine.dispose()


async def test_the_migration_left_a_catalog_behind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A schema without a catalog resolves nothing, so migration 0005 seeds."""
    assert await _count(session_factory, Exercise) == len(CATALOG_SEED)
    assert await _count(session_factory, ExerciseRelation) == len(SEED_RELATIONS)
    assert await _count(session_factory, Muscle) > 0
    assert await _count(session_factory, Equipment) > 0


async def test_seeding_again_changes_nothing(
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`make seed` reconciles an existing database, so it must converge rather
    than accumulate — the same property provisioning.py has."""
    before = {
        model.__name__: await _count(session_factory, model)
        for model in (Exercise, ExerciseAlias, ExerciseMuscle, ExerciseEquipment, ExerciseRelation)
    }

    report = await _reseed(migrated_database)

    after = {
        model.__name__: await _count(session_factory, model)
        for model in (Exercise, ExerciseAlias, ExerciseMuscle, ExerciseEquipment, ExerciseRelation)
    }

    assert before == after
    assert report.exercises_inserted == 0
    assert report.aliases_inserted == 0
    assert report.exercises_updated == len(CATALOG_SEED)


async def test_the_seeded_identity_of_every_exercise_is_stable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        rows = (await session.scalars(sa.select(Exercise))).all()

    assert {row.slug for row in rows} == {exercise.slug for exercise in CATALOG_SEED}
    assert {row.canonical_name for row in rows} == {
        exercise.canonical_name for exercise in CATALOG_SEED
    }


async def test_a_duplicate_canonical_name_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(
                Exercise(
                    canonical_name="Supino reto",
                    slug="supino-reto-duplicado",
                    activity_type=ActivityType.STRENGTH,
                    default_load_mode=LoadMode.TOTAL,
                )
            )


async def test_two_global_aliases_with_the_same_text_are_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The partial index is the whole point: a plain UNIQUE(user_id, alias)
    would accept this, because PostgreSQL treats NULLs as distinct — and the
    resolver's stage 2 would have to choose between two answers."""
    exercise_id = await _some_exercise(session_factory)

    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(
                ExerciseAlias(
                    exercise_id=exercise_id,
                    user_id=None,
                    alias="Supino",
                    normalized_alias=normalize_for_match("supino"),
                    source=AliasSource.SEED,
                )
            )


async def test_a_user_alias_may_repeat_a_global_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Someone whose "supino" means the incline variant must be able to say so
    without the global alias being in the way (§16, stage 1 beats stage 2)."""
    exercise_id = await _some_exercise(session_factory, slug="supino-inclinado")

    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        session.add(
            ExerciseAlias(
                exercise_id=exercise_id,
                user_id=user.id,
                alias="supino",
                normalized_alias=normalize_for_match("supino"),
                source=AliasSource.USER_CONFIRMED,
            )
        )
        user_id = user.id

    async with session_factory() as session:
        stored = (
            await session.scalars(sa.select(ExerciseAlias).where(ExerciseAlias.user_id == user_id))
        ).one()

    assert stored.normalized_alias == "supino"


async def test_two_users_may_learn_the_same_alias(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    exercise_id = await _some_exercise(session_factory, slug="leg-press")

    async with unit_of_work(session_factory) as session:
        for _ in range(2):
            user = User()
            session.add(user)
            await session.flush()
            session.add(
                ExerciseAlias(
                    exercise_id=exercise_id,
                    user_id=user.id,
                    alias="meu leg",
                    normalized_alias=normalize_for_match("meu leg"),
                    source=AliasSource.USER_CONFIRMED,
                )
            )

    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(ExerciseAlias)
            .where(ExerciseAlias.normalized_alias == "meu leg")
        )

    assert count == 2


async def test_a_soft_deleted_global_alias_frees_its_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The index excludes deleted rows, so a retired alias does not block the
    name forever — which matters the first time a curation mistake is fixed."""
    from datetime import UTC, datetime

    exercise_id = await _some_exercise(session_factory)

    async with unit_of_work(session_factory) as session:
        session.add(
            ExerciseAlias(
                exercise_id=exercise_id,
                user_id=None,
                alias="temporário",
                normalized_alias="temporario",
                source=AliasSource.SEED,
            )
        )

    async with unit_of_work(session_factory) as session:
        alias = (
            await session.scalars(
                sa.select(ExerciseAlias).where(ExerciseAlias.normalized_alias == "temporario")
            )
        ).one()
        alias.deleted_at = datetime.now(UTC)

    async with unit_of_work(session_factory) as session:
        session.add(
            ExerciseAlias(
                exercise_id=exercise_id,
                user_id=None,
                alias="temporario",
                normalized_alias="temporario",
                source=AliasSource.SEED,
            )
        )

    async with session_factory() as session:
        alive = await session.scalar(
            sa.select(sa.func.count())
            .select_from(ExerciseAlias)
            .where(
                ExerciseAlias.normalized_alias == "temporario",
                ExerciseAlias.deleted_at.is_(None),
            )
        )

    assert alive == 1


async def test_every_seeded_exercise_has_a_primary_muscle_in_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Asserted against the table rather than the fixture: the seed could be
    right and the loader still drop a role."""
    async with session_factory() as session:
        without_primary = (
            await session.scalars(
                sa.select(Exercise.slug).where(
                    ~sa.exists().where(
                        ExerciseMuscle.exercise_id == Exercise.id,
                        ExerciseMuscle.role == MuscleRole.PRIMARY,
                    )
                )
            )
        ).all()

    assert list(without_primary) == []


async def test_an_exercise_cannot_relate_to_itself(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.exercises.catalog import ExerciseRelationType

    exercise_id = await _some_exercise(session_factory)

    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(
                ExerciseRelation(
                    from_exercise_id=exercise_id,
                    to_exercise_id=exercise_id,
                    relation_type=ExerciseRelationType.VARIATION_OF,
                )
            )


async def test_the_dumbbell_row_carries_the_flag_that_makes_q49_mechanical(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        dumbbells = (
            await session.scalars(sa.select(Equipment).where(Equipment.slug == "halteres"))
        ).one()

    assert dumbbells.is_implement is True


def test_migration_0005_applies_and_downgrades_cleanly(
    settings: ApplicationSettings,
) -> None:
    """Including the seed: a downgrade that leaves catalog rows behind would
    make the next upgrade insert them twice."""
    from alembic import command

    config = alembic_config(settings)

    command.downgrade(config, "0004_one_batch_per_message")
    command.upgrade(config, "head")


async def _some_exercise(
    session_factory: async_sessionmaker[AsyncSession], slug: str = "supino-reto"
) -> UUID:
    async with session_factory() as session:
        exercise = (await session.scalars(sa.select(Exercise).where(Exercise.slug == slug))).one()
    return exercise.id


# --------------------------------------------------------------------------
# Convergence: a correction has to reach a database that already ran the seed
# --------------------------------------------------------------------------


async def test_reseeding_retargets_an_alias_that_moved(
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A curation fix that moves an alias to another exercise must land on
    existing databases too. Skipping the conflict would leave them resolving
    "supino" to the wrong movement forever, while fresh ones got it right."""
    wrong_target = await _some_exercise(session_factory, slug="leg-press")

    async with unit_of_work(session_factory) as session:
        alias = (
            await session.scalars(
                sa.select(ExerciseAlias).where(ExerciseAlias.normalized_alias == "supino")
            )
        ).one()
        alias.exercise_id = wrong_target

    await _reseed(migrated_database)

    async with session_factory() as session:
        alias = (
            await session.scalars(
                sa.select(ExerciseAlias).where(ExerciseAlias.normalized_alias == "supino")
            )
        ).one()
        correct = (
            await session.scalars(sa.select(Exercise).where(Exercise.slug == "supino-reto"))
        ).one()

    assert alias.exercise_id == correct.id


async def test_reseeding_retires_an_alias_the_catalog_dropped(
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Soft-deleted rather than deleted: the text is freed by the partial index
    while the row still explains a resolution somebody acted on last month."""
    exercise_id = await _some_exercise(session_factory)

    async with unit_of_work(session_factory) as session:
        session.add(
            ExerciseAlias(
                exercise_id=exercise_id,
                user_id=None,
                alias="alias que saiu da curadoria",
                normalized_alias="alias que saiu da curadoria",
                source=AliasSource.SEED,
            )
        )

    report = await _reseed(migrated_database)

    async with session_factory() as session:
        retired = (
            await session.scalars(
                sa.select(ExerciseAlias).where(
                    ExerciseAlias.normalized_alias == "alias que saiu da curadoria"
                )
            )
        ).one()

    assert report.aliases_retired == 1
    assert retired.deleted_at is not None


async def test_reseeding_does_not_touch_aliases_a_user_learned(
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Retirement is scoped to seed-owned global rows. A user's own alias is
    not the catalog's to withdraw."""
    exercise_id = await _some_exercise(session_factory, slug="leg-press")

    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        session.add(
            ExerciseAlias(
                exercise_id=exercise_id,
                user_id=user.id,
                alias="minha prensa",
                normalized_alias="minha prensa",
                source=AliasSource.USER_CONFIRMED,
            )
        )

    await _reseed(migrated_database)

    async with session_factory() as session:
        learned = (
            await session.scalars(
                sa.select(ExerciseAlias).where(ExerciseAlias.normalized_alias == "minha prensa")
            )
        ).one()

    assert learned.deleted_at is None


async def test_reseeding_corrects_a_muscle_role(
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A muscle promoted from secondary to primary changes the role on an
    existing pair. Skipping the conflict would make an old database group its
    analysis differently from a new one."""
    async with unit_of_work(session_factory) as session:
        exercise = (
            await session.scalars(sa.select(Exercise).where(Exercise.slug == "supino-reto"))
        ).one()
        link = (
            await session.scalars(
                sa.select(ExerciseMuscle).where(
                    ExerciseMuscle.exercise_id == exercise.id,
                    ExerciseMuscle.role == MuscleRole.PRIMARY,
                )
            )
        ).one()
        link.role = MuscleRole.STABILIZER
        link_id = link.id

    await _reseed(migrated_database)

    async with session_factory() as session:
        corrected = await session.get(ExerciseMuscle, link_id)

    assert corrected is not None
    assert corrected.role is MuscleRole.PRIMARY


async def test_reseeding_removes_a_muscle_link_the_catalog_dropped(
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with unit_of_work(session_factory) as session:
        exercise = (
            await session.scalars(sa.select(Exercise).where(Exercise.slug == "leg-press"))
        ).one()
        muscle = (await session.scalars(sa.select(Muscle).where(Muscle.slug == "biceps"))).one()
        session.add(
            ExerciseMuscle(exercise_id=exercise.id, muscle_id=muscle.id, role=MuscleRole.SECONDARY)
        )
        exercise_id, muscle_id = exercise.id, muscle.id

    report = await _reseed(migrated_database)

    async with session_factory() as session:
        remaining = await session.scalar(
            sa.select(sa.func.count())
            .select_from(ExerciseMuscle)
            .where(
                ExerciseMuscle.exercise_id == exercise_id,
                ExerciseMuscle.muscle_id == muscle_id,
            )
        )

    assert remaining == 0
    assert report.links_removed >= 1


async def test_reseeding_retires_an_exercise_the_catalog_dropped(
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Soft, always: sets somebody logged point at this row, so a hard delete
    would either fail on the foreign key or take the history with it."""
    async with unit_of_work(session_factory) as session:
        session.add(
            Exercise(
                canonical_name="Exercício removido da curadoria",
                slug="exercicio-removido",
                activity_type=ActivityType.STRENGTH,
                default_load_mode=LoadMode.TOTAL,
            )
        )

    await _reseed(migrated_database)

    async with session_factory() as session:
        retired = (
            await session.scalars(sa.select(Exercise).where(Exercise.slug == "exercicio-removido"))
        ).one()

    assert retired.deleted_at is not None
