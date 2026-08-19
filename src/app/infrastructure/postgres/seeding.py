"""Loading the curated catalog into a database (§16, WS-1).

Convergent and re-runnable, for the reason `provisioning.py` records: a
migration runs once and is stamped forever, so a catalog seeded only by
migration `0005` would reach fresh databases and never the ones already
running. Migration `0005` calls this so a clean clone has a catalog; `make seed`
calls it again whenever the curated data changes.

Idempotence is `ON CONFLICT (slug) DO UPDATE` on the three entity tables and
`ON CONFLICT DO NOTHING` on the join tables and aliases. Updating rather than
skipping matters: a corrected canonical name has to reach databases that
already have the old one.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from app.domain.exercises.catalog import AliasSource, MuscleRole, SeedExercise
from app.domain.exercises.catalog_data import (
    CATALOG_SEED,
    SEED_EQUIPMENT,
    SEED_MUSCLES,
    SEED_RELATIONS,
)
from app.domain.identifiers import new_uuid7
from app.infrastructure.postgres.models import (
    Equipment,
    Exercise,
    ExerciseAlias,
    ExerciseEquipment,
    ExerciseMuscle,
    ExerciseRelation,
    Muscle,
)

logger = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")


def normalize_for_match(text: str) -> str:
    """Casefold, strip accents, drop punctuation, collapse whitespace.

    The resolver matches on this form, so it is stored rather than computed at
    query time: `supino` typed with or without an accent, in any case, has to
    reach the same index entry. WS-7 reuses this exact function, which is why
    it lives with the loader rather than inside it.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_punctuation = _PUNCTUATION.sub(" ", without_accents)
    return _WHITESPACE.sub(" ", without_punctuation).strip().casefold()


@dataclass(frozen=True, slots=True)
class SeedReport:
    exercises_inserted: int
    exercises_updated: int
    aliases_inserted: int

    @property
    def changed(self) -> bool:
        return bool(self.exercises_inserted or self.aliases_inserted)


def seed_catalog_sync(connection: Connection) -> SeedReport:
    """Apply the curated catalog to a database, in one connection's work.

    Synchronous because Alembic hands migrations a sync connection, and having
    one implementation avoids the seed drifting between the migration path and
    the `make seed` path.
    """
    muscle_ids = _upsert_lookup(
        connection,
        Muscle,
        [
            {"slug": muscle.slug, "name": muscle.name, "muscle_group": muscle.group}
            for muscle in SEED_MUSCLES
        ],
        updatable=("name", "muscle_group"),
    )
    equipment_ids = _upsert_lookup(
        connection,
        Equipment,
        [
            {"slug": item.slug, "name": item.name, "is_implement": item.is_implement}
            for item in SEED_EQUIPMENT
        ],
        updatable=("name", "is_implement"),
    )

    exercise_ids, inserted, updated = _upsert_exercises(connection)
    aliases_inserted = _insert_aliases(connection, exercise_ids)
    _insert_muscle_links(connection, exercise_ids, muscle_ids)
    _insert_equipment_links(connection, exercise_ids, equipment_ids)
    _insert_relations(connection, exercise_ids)

    report = SeedReport(
        exercises_inserted=inserted,
        exercises_updated=updated,
        aliases_inserted=aliases_inserted,
    )
    logger.info(
        "catalog seeded",
        extra={
            "exercises_inserted": report.exercises_inserted,
            "exercises_updated": report.exercises_updated,
            "aliases_inserted": report.aliases_inserted,
        },
    )
    return report


def _upsert_lookup(
    connection: Connection,
    model: type[Muscle] | type[Equipment],
    rows: list[dict[str, Any]],
    *,
    updatable: tuple[str, ...],
) -> dict[str, UUID]:
    for row in rows:
        statement = insert(model).values(id=new_uuid7(), **row)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["slug"],
                set_={column: getattr(statement.excluded, column) for column in updatable},
            )
        )

    return dict(connection.execute(sa.select(model.slug, model.id)).all())  # type: ignore[arg-type]


def _upsert_exercises(connection: Connection) -> tuple[dict[str, UUID], int, int]:
    existing = {slug for (slug,) in connection.execute(sa.select(Exercise.slug)).all()}

    for exercise in CATALOG_SEED:
        statement = insert(Exercise).values(
            id=new_uuid7(),
            slug=exercise.slug,
            canonical_name=exercise.canonical_name,
            activity_type=exercise.activity_type,
            default_load_mode=exercise.default_load_mode,
            is_bodyweight=exercise.is_bodyweight,
            locale="pt-BR",
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["slug"],
                set_={
                    "canonical_name": statement.excluded.canonical_name,
                    "activity_type": statement.excluded.activity_type,
                    "default_load_mode": statement.excluded.default_load_mode,
                    "is_bodyweight": statement.excluded.is_bodyweight,
                },
            )
        )

    ids: dict[str, UUID] = dict(
        connection.execute(sa.select(Exercise.slug, Exercise.id)).all()  # type: ignore[arg-type]
    )
    seeded = {exercise.slug for exercise in CATALOG_SEED}
    return ids, len(seeded - existing), len(seeded & existing)


def _insert_aliases(connection: Connection, exercise_ids: dict[str, UUID]) -> int:
    inserted = 0
    for exercise in CATALOG_SEED:
        for alias in _unique_aliases(exercise):
            statement = insert(ExerciseAlias).values(
                id=new_uuid7(),
                exercise_id=exercise_ids[exercise.slug],
                user_id=None,
                alias=alias,
                normalized_alias=normalize_for_match(alias),
                source=AliasSource.SEED,
            )
            result = connection.execute(
                # The unique index is partial, so PostgreSQL cannot infer it
                # from the column alone: the predicate has to match the index's
                # own, or the statement fails with "no unique or exclusion
                # constraint matching the ON CONFLICT specification".
                statement.on_conflict_do_nothing(
                    index_elements=["normalized_alias"],
                    index_where=sa.text("user_id IS NULL AND deleted_at IS NULL"),
                )
            )
            # `rowcount` is -1, not 0, when ON CONFLICT DO NOTHING skips a
            # row, so a plain sum reports a negative number of insertions.
            inserted += 1 if result.rowcount == 1 else 0
    return inserted


def _unique_aliases(exercise: SeedExercise) -> list[str]:
    """The exercise's aliases plus its canonical name, deduplicated by normal form.

    The canonical name is an alias too: stage 3 of the resolver would find it
    anyway, but having it in one index keeps the lookup a single query.
    """
    seen: dict[str, str] = {}
    for alias in (exercise.canonical_name, *exercise.aliases):
        seen.setdefault(normalize_for_match(alias), alias)
    return list(seen.values())


def _insert_muscle_links(
    connection: Connection, exercise_ids: dict[str, UUID], muscle_ids: dict[str, UUID]
) -> None:
    for exercise in CATALOG_SEED:
        roles = (
            (exercise.primary_muscles, MuscleRole.PRIMARY),
            (exercise.secondary_muscles, MuscleRole.SECONDARY),
            (exercise.stabilizer_muscles, MuscleRole.STABILIZER),
        )
        for slugs, role in roles:
            for slug in slugs:
                connection.execute(
                    insert(ExerciseMuscle)
                    .values(
                        id=new_uuid7(),
                        exercise_id=exercise_ids[exercise.slug],
                        muscle_id=muscle_ids[slug],
                        role=role,
                    )
                    .on_conflict_do_nothing(constraint="uq_exercise_muscles_pair")
                )


def _insert_equipment_links(
    connection: Connection, exercise_ids: dict[str, UUID], equipment_ids: dict[str, UUID]
) -> None:
    for exercise in CATALOG_SEED:
        for position, slug in enumerate(exercise.equipment):
            connection.execute(
                insert(ExerciseEquipment)
                .values(
                    id=new_uuid7(),
                    exercise_id=exercise_ids[exercise.slug],
                    equipment_id=equipment_ids[slug],
                    is_primary=position == 0,
                )
                .on_conflict_do_nothing(constraint="uq_exercise_equipment_pair")
            )


def _insert_relations(connection: Connection, exercise_ids: dict[str, UUID]) -> None:
    for relation in SEED_RELATIONS:
        connection.execute(
            insert(ExerciseRelation)
            .values(
                id=new_uuid7(),
                from_exercise_id=exercise_ids[relation.from_slug],
                to_exercise_id=exercise_ids[relation.to_slug],
                relation_type=relation.relation_type,
            )
            .on_conflict_do_nothing(constraint="uq_exercise_relations")
        )


def main() -> None:  # pragma: no cover - thin entrypoint, exercised via seed_catalog_sync
    """Reconcile an existing database with the curated catalog (`make seed`)."""
    from sqlalchemy import create_engine

    from app.config import load_settings

    settings = load_settings()
    engine = create_engine(settings.postgres.admin_dsn().replace("+asyncpg", "+psycopg"))
    try:
        with engine.begin() as connection:
            report = seed_catalog_sync(connection)
    finally:
        engine.dispose()

    print(  # noqa: T201 - a command's own output
        f"catalog: {report.exercises_inserted} inserted, "
        f"{report.exercises_updated} updated, {report.aliases_inserted} new aliases"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
