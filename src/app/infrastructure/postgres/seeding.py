"""Loading the curated catalog into a database (§16, WS-1).

Convergent and re-runnable, for the reason `provisioning.py` records: a
migration runs once and is stamped forever, so a catalog seeded only by
migration `0005` would reach fresh databases and never the ones already
running. Migration `0005` calls this so a clean clone has a catalog; `make seed`
calls it again whenever the curated data changes.

Convergent, not merely additive. Every conflict updates rather than skips, and
rows the seed no longer contains are retired. That distinction is the whole
point: an alias moved to another exercise, a muscle promoted from secondary to
primary, or an entry removed after a curation mistake must reach databases that
already have the old version. A seed that only inserts leaves those databases
resolving user input to the wrong exercise indefinitely, while fresh ones get
the correction -- the same divergence this module exists to prevent.

Removal is soft wherever history can point at the row: an exercise somebody
already logged sets against is retired, never deleted.
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
    aliases_retired: int = 0
    links_removed: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.exercises_inserted
            or self.aliases_inserted
            or self.aliases_retired
            or self.links_removed
        )


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
    aliases_inserted, aliases_retired = _sync_aliases(connection, exercise_ids)
    removed = _sync_muscle_links(connection, exercise_ids, muscle_ids)
    removed += _sync_equipment_links(connection, exercise_ids, equipment_ids)
    removed += _sync_relations(connection, exercise_ids)
    removed += _retire_stale_exercises(connection)

    report = SeedReport(
        exercises_inserted=inserted,
        exercises_updated=updated,
        aliases_inserted=aliases_inserted,
        aliases_retired=aliases_retired,
        links_removed=removed,
    )
    logger.info(
        "catalog seeded",
        extra={
            "exercises_inserted": report.exercises_inserted,
            "exercises_updated": report.exercises_updated,
            "aliases_inserted": report.aliases_inserted,
            "aliases_retired": report.aliases_retired,
            "links_removed": report.links_removed,
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


def _unique_aliases(exercise: SeedExercise) -> list[str]:
    """The exercise's aliases plus its canonical name, deduplicated by normal form.

    The canonical name is an alias too: stage 3 of the resolver would find it
    anyway, but having it in one index keeps the lookup a single query.
    """
    seen: dict[str, str] = {}
    for alias in (exercise.canonical_name, *exercise.aliases):
        seen.setdefault(normalize_for_match(alias), alias)
    return list(seen.values())


def _sync_aliases(connection: Connection, exercise_ids: dict[str, UUID]) -> tuple[int, int]:
    """Upsert every seeded alias and retire the ones the seed dropped.

    The conflict target is `normalized_alias` alone, so a correction that moves
    an alias to another exercise has to *update* `exercise_id` -- skipping
    would leave the database pointing at the old movement forever, which is
    precisely the silent wrong-exercise failure the catalog is written to avoid.
    """
    desired: dict[str, tuple[UUID, str]] = {}
    for exercise in CATALOG_SEED:
        for alias in _unique_aliases(exercise):
            desired[normalize_for_match(alias)] = (exercise_ids[exercise.slug], alias)

    # Counted against what was already there: ON CONFLICT DO UPDATE reports one
    # affected row whether it inserted or updated, so the statement cannot tell
    # us which happened.
    existing = {
        normalized
        for (normalized,) in connection.execute(
            sa.select(ExerciseAlias.normalized_alias).where(
                ExerciseAlias.user_id.is_(None), ExerciseAlias.deleted_at.is_(None)
            )
        ).all()
    }
    inserted = len(set(desired) - existing)

    for normalized, (exercise_id, alias) in desired.items():
        statement = insert(ExerciseAlias).values(
            id=new_uuid7(),
            exercise_id=exercise_id,
            user_id=None,
            alias=alias,
            normalized_alias=normalized,
            source=AliasSource.SEED,
        )
        connection.execute(
            # The unique index is partial, so PostgreSQL cannot infer it from
            # the column alone: the predicate has to match the index's own, or
            # the statement fails with "no unique or exclusion constraint
            # matching the ON CONFLICT specification".
            statement.on_conflict_do_update(
                index_elements=["normalized_alias"],
                index_where=sa.text("user_id IS NULL AND deleted_at IS NULL"),
                set_={
                    "exercise_id": statement.excluded.exercise_id,
                    "alias": statement.excluded.alias,
                    "source": statement.excluded.source,
                },
            )
        )

    retired = _retire_stale_aliases(connection, set(desired))
    return inserted, retired


def _retire_stale_aliases(connection: Connection, desired: set[str]) -> int:
    """Soft-delete global aliases the seed no longer claims.

    Soft, not hard: the partial unique index excludes deleted rows, so the text
    is freed for reuse while the row stays available to explain a resolution
    somebody made a decision on last month.
    """
    result = connection.execute(
        sa.update(ExerciseAlias)
        .where(
            ExerciseAlias.user_id.is_(None),
            ExerciseAlias.source == AliasSource.SEED,
            ExerciseAlias.deleted_at.is_(None),
            ExerciseAlias.normalized_alias.not_in(desired) if desired else sa.true(),
        )
        .values(deleted_at=sa.func.now())
    )
    return int(result.rowcount or 0)


def _sync_muscle_links(
    connection: Connection, exercise_ids: dict[str, UUID], muscle_ids: dict[str, UUID]
) -> int:
    """Upsert the roles and drop links the seed dropped.

    A muscle promoted from secondary to primary changes the *role* on an
    existing pair, so the conflict has to update it. Skipping would leave an
    existing database grouping analysis differently from a fresh one.
    """
    desired: set[tuple[UUID, UUID]] = set()

    for exercise in CATALOG_SEED:
        roles = (
            (exercise.primary_muscles, MuscleRole.PRIMARY),
            (exercise.secondary_muscles, MuscleRole.SECONDARY),
            (exercise.stabilizer_muscles, MuscleRole.STABILIZER),
        )
        for slugs, role in roles:
            for slug in slugs:
                exercise_id, muscle_id = exercise_ids[exercise.slug], muscle_ids[slug]
                desired.add((exercise_id, muscle_id))
                statement = insert(ExerciseMuscle).values(
                    id=new_uuid7(),
                    exercise_id=exercise_id,
                    muscle_id=muscle_id,
                    role=role,
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        constraint="uq_exercise_muscles_pair",
                        set_={"role": statement.excluded.role},
                    )
                )

    return _delete_stale_muscle_links(connection, exercise_ids, desired)


def _retire_stale_exercises(connection: Connection) -> int:
    """Soft-delete exercises the catalog no longer contains.

    Soft, always: sets somebody logged last month point at these rows, and a
    hard delete would either fail on the foreign key or take the history with
    it. A retired exercise stops being resolvable and stays explainable.
    """
    slugs = [exercise.slug for exercise in CATALOG_SEED]
    result = connection.execute(
        sa.update(Exercise)
        .where(Exercise.slug.not_in(slugs), Exercise.deleted_at.is_(None))
        .values(deleted_at=sa.func.now())
    )
    return int(result.rowcount or 0)


def _sync_equipment_links(
    connection: Connection, exercise_ids: dict[str, UUID], equipment_ids: dict[str, UUID]
) -> int:
    desired: set[tuple[UUID, UUID]] = set()

    for exercise in CATALOG_SEED:
        for position, slug in enumerate(exercise.equipment):
            exercise_id, equipment_id = exercise_ids[exercise.slug], equipment_ids[slug]
            desired.add((exercise_id, equipment_id))
            statement = insert(ExerciseEquipment).values(
                id=new_uuid7(),
                exercise_id=exercise_id,
                equipment_id=equipment_id,
                is_primary=position == 0,
            )
            connection.execute(
                statement.on_conflict_do_update(
                    constraint="uq_exercise_equipment_pair",
                    set_={"is_primary": statement.excluded.is_primary},
                )
            )

    return _delete_stale_equipment_links(connection, exercise_ids, desired)


def _delete_stale_muscle_links(
    connection: Connection,
    exercise_ids: dict[str, UUID],
    desired: set[tuple[UUID, UUID]],
) -> int:
    """Remove muscle links for seeded exercises that the seed no longer declares.

    Hard delete: a join row carries no history of its own, and leaving a muscle
    attached after curation removed it is what makes two databases disagree.
    Only seeded exercises are touched.
    """
    seeded = set(exercise_ids.values())
    found = connection.execute(
        sa.select(ExerciseMuscle.id, ExerciseMuscle.exercise_id, ExerciseMuscle.muscle_id).where(
            ExerciseMuscle.exercise_id.in_(seeded)
        )
    ).all()
    stale = [row[0] for row in found if (row[1], row[2]) not in desired]
    if not stale:
        return 0

    result = connection.execute(sa.delete(ExerciseMuscle).where(ExerciseMuscle.id.in_(stale)))
    return int(result.rowcount or 0)


def _delete_stale_equipment_links(
    connection: Connection,
    exercise_ids: dict[str, UUID],
    desired: set[tuple[UUID, UUID]],
) -> int:
    seeded = set(exercise_ids.values())
    found = connection.execute(
        sa.select(
            ExerciseEquipment.id,
            ExerciseEquipment.exercise_id,
            ExerciseEquipment.equipment_id,
        ).where(ExerciseEquipment.exercise_id.in_(seeded))
    ).all()
    stale = [row[0] for row in found if (row[1], row[2]) not in desired]
    if not stale:
        return 0

    result = connection.execute(sa.delete(ExerciseEquipment).where(ExerciseEquipment.id.in_(stale)))
    return int(result.rowcount or 0)


def _sync_relations(connection: Connection, exercise_ids: dict[str, UUID]) -> int:
    desired: set[tuple[UUID, UUID, str]] = set()

    for relation in SEED_RELATIONS:
        from_id = exercise_ids[relation.from_slug]
        to_id = exercise_ids[relation.to_slug]
        desired.add((from_id, to_id, relation.relation_type.value))
        connection.execute(
            insert(ExerciseRelation)
            .values(
                id=new_uuid7(),
                from_exercise_id=from_id,
                to_exercise_id=to_id,
                relation_type=relation.relation_type,
            )
            .on_conflict_do_nothing(constraint="uq_exercise_relations")
        )

    seeded = set(exercise_ids.values())
    rows = connection.execute(
        sa.select(
            ExerciseRelation.id,
            ExerciseRelation.from_exercise_id,
            ExerciseRelation.to_exercise_id,
            ExerciseRelation.relation_type,
        ).where(ExerciseRelation.from_exercise_id.in_(seeded))
    ).all()
    stale = [
        identifier
        for identifier, from_id, to_id, relation_type in rows
        if (from_id, to_id, str(relation_type)) not in desired
    ]
    if not stale:
        return 0

    result = connection.execute(sa.delete(ExerciseRelation).where(ExerciseRelation.id.in_(stale)))
    return int(result.rowcount or 0)


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
