"""The exercise catalog, read from PostgreSQL (§16, Q49).

Four lookups and a listing — §16's stages and nothing else. The staged logic
lives in the resolver, which never learns this class exists; what lives here is
the SQL. The alias stages are index seeks on `normalized_alias`. The canonical
stage is not: canonical names are stored as written, and PostgreSQL cannot
reproduce this normalization without `unaccent`, so it matches in Python over
the same cached listing the fuzzy stage already needs.

`uses_implements` is computed here rather than stored on the exercise, because
it is a fact about the equipment: `equipment.is_implement` is what makes Q49's
"60kg de halteres" mechanical instead of a name-matching guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.exercise_catalog import CatalogEntry, SearchableExercise
from app.domain.exercises.normalization import normalize_for_match
from app.infrastructure.postgres.models import (
    Equipment,
    Exercise,
    ExerciseAlias,
    ExerciseEquipment,
)


def _entry(row: sa.Row[tuple[Exercise, bool]]) -> CatalogEntry:
    exercise, uses_implements = row
    return CatalogEntry(
        exercise_id=exercise.id,
        canonical_name=exercise.canonical_name,
        activity_type=exercise.activity_type,
        default_load_mode=exercise.default_load_mode,
        is_bodyweight=exercise.is_bodyweight,
        uses_implements=uses_implements,
    )


#: Whether any of the exercise's equipment is held per side (Q49).
_USES_IMPLEMENTS = (
    sa.select(sa.func.bool_or(Equipment.is_implement))
    .select_from(ExerciseEquipment)
    .join(Equipment, Equipment.id == ExerciseEquipment.equipment_id)
    .where(ExerciseEquipment.exercise_id == Exercise.id)
    .correlate(Exercise)
    .scalar_subquery()
)


class PostgresExerciseCatalog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        #: The catalog is ~40 immutable rows within a request, and the fuzzy
        #: stage reads all of them for every unresolved name. Cached per
        #: instance rather than per process, so an alias learned in Sprint 3 is
        #: visible on the user's next message instead of after a redeploy.
        self._searchable: tuple[SearchableExercise, ...] | None = None

    def _base(self) -> sa.Select[tuple[Exercise, bool]]:
        return sa.select(Exercise, sa.func.coalesce(_USES_IMPLEMENTS, False)).where(
            Exercise.deleted_at.is_(None)
        )

    async def by_user_alias(self, normalized: str, user_id: UUID) -> CatalogEntry | None:
        """Stage 1: what this user has always meant by that word (§16)."""
        return await self._one(
            self._base()
            .join(ExerciseAlias, ExerciseAlias.exercise_id == Exercise.id)
            .where(
                ExerciseAlias.normalized_alias == normalized,
                ExerciseAlias.user_id == user_id,
                ExerciseAlias.deleted_at.is_(None),
            )
        )

    async def by_global_alias(self, normalized: str) -> CatalogEntry | None:
        """Stage 2: what the word means to everybody."""
        return await self._one(
            self._base()
            .join(ExerciseAlias, ExerciseAlias.exercise_id == Exercise.id)
            .where(
                ExerciseAlias.normalized_alias == normalized,
                ExerciseAlias.user_id.is_(None),
                ExerciseAlias.deleted_at.is_(None),
            )
        )

    async def by_canonical_name(self, normalized: str) -> CatalogEntry | None:
        """Stage 3: the catalog's own name for it.

        Matched in Python against the cached listing rather than in SQL:
        `canonical_name` is stored as written, and an index on it would be an
        index on a different string than the one being searched for.
        """
        for searchable in await self.all_searchable():
            if normalize_for_match(searchable.entry.canonical_name) == normalized:
                return searchable.entry
        return None

    async def all_searchable(self) -> Sequence[SearchableExercise]:
        """Every exercise with every term it can be reached by.

        Global aliases only: `all_searchable` feeds the fuzzy stage, which runs
        for whoever is asking, and one user's private shorthand appearing here
        would resolve another user's typo to an exercise they never named.
        """
        if self._searchable is not None:
            return self._searchable

        rows = (await self._session.execute(self._base())).all()
        aliases = (
            await self._session.execute(
                sa.select(ExerciseAlias.exercise_id, ExerciseAlias.normalized_alias).where(
                    ExerciseAlias.user_id.is_(None), ExerciseAlias.deleted_at.is_(None)
                )
            )
        ).all()

        by_exercise: dict[UUID, list[str]] = {}
        for exercise_id, normalized_alias in aliases:
            by_exercise.setdefault(exercise_id, []).append(normalized_alias)

        searchable: list[SearchableExercise] = []
        for row in rows:
            entry = _entry(row)
            terms = [normalize_for_match(entry.canonical_name)]
            terms.extend(by_exercise.get(entry.exercise_id, ()))
            searchable.append(
                SearchableExercise(entry=entry, normalized_terms=tuple(dict.fromkeys(terms)))
            )

        self._searchable = tuple(searchable)
        return self._searchable

    async def _one(self, statement: sa.Select[tuple[Exercise, bool]]) -> CatalogEntry | None:
        row = (await self._session.execute(statement.limit(1))).first()
        return _entry(row) if row is not None else None
