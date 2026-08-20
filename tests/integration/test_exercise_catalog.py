"""WS-7: the catalog adapter against the seeded database (§16, Q49).

The resolver's stage order is tested in memory, where a database cannot hide a
mistake in it. What has to be tested here is the opposite: that the four
lookups find the rows migration 0005 actually seeded, in the normalized form
the resolver will ask for.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.exercises.catalog import AliasSource
from app.domain.exercises.normalization import normalize_for_match
from app.domain.training.activities import ActivityType, LoadMode
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.exercise_catalog import PostgresExerciseCatalog
from app.infrastructure.postgres.models import Exercise, ExerciseAlias, User

pytestmark = [pytest.mark.integration]


async def test_a_global_alias_finds_its_exercise(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        catalog = PostgresExerciseCatalog(session)
        found = await catalog.by_global_alias(normalize_for_match("bench press"))

    assert found is not None
    assert found.canonical_name == "Supino reto"
    assert found.activity_type is ActivityType.STRENGTH


async def test_the_canonical_name_finds_its_exercise(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        catalog = PostgresExerciseCatalog(session)
        found = await catalog.by_canonical_name(normalize_for_match("Levantamento terra romeno"))

    assert found is not None
    assert found.canonical_name == "Levantamento terra romeno"


async def test_a_users_alias_is_visible_only_to_that_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sprint 3 writes these when somebody answers a clarification. The point
    of the column is that one person's shorthand is not everybody's."""
    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        incline_id = (
            await session.scalars(sa.select(Exercise.id).where(Exercise.slug == "supino-inclinado"))
        ).one()
        session.add(
            ExerciseAlias(
                exercise_id=incline_id,
                user_id=user.id,
                alias="meu supino",
                normalized_alias=normalize_for_match("meu supino"),
                source=AliasSource.USER_CONFIRMED,
            )
        )
        user_id = user.id

    async with session_factory() as session:
        catalog = PostgresExerciseCatalog(session)
        mine = await catalog.by_user_alias(normalize_for_match("meu supino"), user_id)
        theirs = await catalog.by_user_alias(normalize_for_match("meu supino"), uuid4())
        globally = await catalog.by_global_alias(normalize_for_match("meu supino"))

    assert mine is not None
    assert mine.canonical_name == "Supino inclinado"
    assert theirs is None
    assert globally is None, "a personal alias must not leak into the shared namespace"


async def test_a_missing_name_is_absent_rather_than_approximate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        catalog = PostgresExerciseCatalog(session)

        assert await catalog.by_global_alias("nao existe") is None
        assert await catalog.by_canonical_name("nao existe") is None


async def test_implements_are_read_from_the_equipment_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Q49: "60kg de halteres" means 60 per hand or 60 total, and the answer
    comes from `equipment.is_implement` rather than from the exercise's name."""
    async with session_factory() as session:
        catalog = PostgresExerciseCatalog(session)
        dumbbell = await catalog.by_global_alias(normalize_for_match("supino com halteres"))
        barbell = await catalog.by_global_alias(normalize_for_match("supino com barra"))

    assert dumbbell is not None and barbell is not None
    assert dumbbell.uses_implements is True
    assert barbell.uses_implements is False
    assert dumbbell.default_load_mode is LoadMode.PER_IMPLEMENT


async def test_every_seeded_exercise_is_searchable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fuzzy matching scores against aliases as well as canonical names: "rdl"
    and "levantamento terra romeno" are the same exercise reached by very
    different strings, and only one of them looks like the row."""
    async with session_factory() as session:
        catalog = PostgresExerciseCatalog(session)
        searchable = await catalog.all_searchable()

    by_name = {item.entry.canonical_name: item for item in searchable}

    assert len(searchable) >= 40
    romanian = by_name["Levantamento terra romeno"]
    assert normalize_for_match("Levantamento terra romeno") in romanian.normalized_terms
    assert "rdl" in romanian.normalized_terms


async def test_a_personal_alias_does_not_widen_everybody_elses_search(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`all_searchable` feeds the fuzzy stage, which runs for whoever is asking.
    A user's private shorthand appearing there would resolve other people's
    typos to an exercise they never named."""
    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        exercise_id = (
            await session.scalars(sa.select(Exercise.id).where(Exercise.slug == "supino-reto"))
        ).one()
        session.add(
            ExerciseAlias(
                exercise_id=exercise_id,
                user_id=user.id,
                alias="o de sempre",
                normalized_alias=normalize_for_match("o de sempre"),
                source=AliasSource.USER_CONFIRMED,
            )
        )

    async with session_factory() as session:
        catalog = PostgresExerciseCatalog(session)
        searchable = await catalog.all_searchable()

    every_term = {term for item in searchable for term in item.normalized_terms}
    assert "o de sempre" not in every_term
