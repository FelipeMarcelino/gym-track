"""WS-1: the curated catalog, checked without a database (§16, D1).

The seed is data somebody wrote by hand, so the mistakes it can contain are the
mistakes hands make: a muscle slug that does not exist, a relation pointing at
a deleted exercise, the same alias on two movements. Each of those is silent
until a user's message resolves to the wrong exercise.
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.domain.exercises.catalog import SeedExercise
from app.domain.exercises.catalog_data import (
    CATALOG_SEED,
    SEED_EQUIPMENT,
    SEED_MUSCLES,
    SEED_RELATIONS,
    SEEDED_ACTIVITY_TYPES,
)
from app.domain.exercises.normalization import normalize_for_match
from app.domain.training.activities import ActivityType

MUSCLE_SLUGS = {muscle.slug for muscle in SEED_MUSCLES}
EQUIPMENT_SLUGS = {item.slug for item in SEED_EQUIPMENT}
EXERCISE_SLUGS = {exercise.slug for exercise in CATALOG_SEED}


def test_the_catalog_is_the_size_the_decision_promised() -> None:
    assert len(CATALOG_SEED) >= 35, "D1 committed to a curated catalog, not a sample"


def test_slugs_are_unique() -> None:
    duplicates = [slug for slug, count in Counter(EXERCISE_SLUGS).items() if count > 1]
    assert not duplicates
    assert len(EXERCISE_SLUGS) == len(CATALOG_SEED)


def test_canonical_names_are_unique() -> None:
    names = Counter(exercise.canonical_name for exercise in CATALOG_SEED)
    assert [name for name, count in names.items() if count > 1] == []


@pytest.mark.parametrize("exercise", CATALOG_SEED, ids=lambda item: item.slug)
def test_every_exercise_has_a_primary_muscle(exercise: SeedExercise) -> None:
    """Analysis groups by primary muscle in a later sprint; an exercise without
    one is invisible to it."""
    assert exercise.primary_muscles


@pytest.mark.parametrize("exercise", CATALOG_SEED, ids=lambda item: item.slug)
def test_referenced_muscles_exist(exercise: SeedExercise) -> None:
    referenced = set(
        exercise.primary_muscles + exercise.secondary_muscles + exercise.stabilizer_muscles
    )
    assert referenced <= MUSCLE_SLUGS, f"unknown muscles: {sorted(referenced - MUSCLE_SLUGS)}"


@pytest.mark.parametrize("exercise", CATALOG_SEED, ids=lambda item: item.slug)
def test_referenced_equipment_exists(exercise: SeedExercise) -> None:
    assert set(exercise.equipment) <= EQUIPMENT_SLUGS


@pytest.mark.parametrize("exercise", CATALOG_SEED, ids=lambda item: item.slug)
def test_a_muscle_has_exactly_one_role_per_exercise(exercise: SeedExercise) -> None:
    """The join carries UNIQUE(exercise_id, muscle_id), so a muscle listed as
    both primary and secondary would silently lose one role at seed time."""
    all_roles = exercise.primary_muscles + exercise.secondary_muscles + exercise.stabilizer_muscles
    repeated = [slug for slug, count in Counter(all_roles).items() if count > 1]
    assert not repeated, f"{exercise.slug} lists {repeated} under more than one role"


def test_no_alias_belongs_to_two_exercises() -> None:
    """Seeded aliases are global and the database refuses a duplicate, so an
    ambiguous word must simply not be an alias. `remada` is absent for exactly
    this reason: there are four of them."""
    owners: dict[str, list[str]] = {}
    for exercise in CATALOG_SEED:
        for alias in (exercise.canonical_name, *exercise.aliases):
            owners.setdefault(normalize_for_match(alias), []).append(exercise.slug)

    shared = {alias: slugs for alias, slugs in owners.items() if len(set(slugs)) > 1}
    assert not shared, f"aliases claimed by more than one exercise: {shared}"


@pytest.mark.parametrize("exercise", CATALOG_SEED, ids=lambda item: item.slug)
def test_every_alias_survives_normalization(exercise: SeedExercise) -> None:
    """An alias that normalizes to nothing can never be matched, so it is not
    an alias — it is a typo the resolver will never reach."""
    for alias in exercise.aliases:
        assert normalize_for_match(alias), f"{exercise.slug} has an alias that normalizes away"


def test_relations_point_at_real_exercises() -> None:
    endpoints = {relation.from_slug for relation in SEED_RELATIONS} | {
        relation.to_slug for relation in SEED_RELATIONS
    }
    assert endpoints <= EXERCISE_SLUGS


def test_no_exercise_relates_to_itself() -> None:
    """The table's CHECK refuses it; catching it here saves a failed migration."""
    assert [r for r in SEED_RELATIONS if r.from_slug == r.to_slug] == []


def test_relations_are_unique() -> None:
    keys = Counter((r.from_slug, r.to_slug, r.relation_type) for r in SEED_RELATIONS)
    assert [key for key, count in keys.items() if count > 1] == []


@pytest.mark.parametrize("activity_type", sorted(SEEDED_ACTIVITY_TYPES))
def test_every_claimed_activity_type_has_an_entry(activity_type: ActivityType) -> None:
    """A type with no catalog entry is a schema that supports something the
    product cannot actually log."""
    assert any(exercise.activity_type is activity_type for exercise in CATALOG_SEED)


def test_other_is_deliberately_not_seeded() -> None:
    """OTHER exists for activities that are *not* in the catalog, so an entry
    typed OTHER would be a modelling failure rather than data."""
    assert ActivityType.OTHER not in SEEDED_ACTIVITY_TYPES
    assert all(exercise.activity_type is not ActivityType.OTHER for exercise in CATALOG_SEED)


def test_implement_exercises_default_to_per_implement_load() -> None:
    """Q49: a dumbbell movement reporting 20 kg means 20 kg in each hand, and
    the catalog is what makes that mechanical rather than a guess."""
    implements = {item.slug for item in SEED_EQUIPMENT if item.is_implement}
    from app.domain.training.activities import LoadMode

    for exercise in CATALOG_SEED:
        if exercise.equipment and exercise.equipment[0] in implements:
            assert exercise.default_load_mode in (
                LoadMode.PER_IMPLEMENT,
                LoadMode.BODYWEIGHT_PLUS,
            ), f"{exercise.slug} is held one per hand but does not default to per-implement"


def test_bodyweight_exercises_declare_a_bodyweight_load_mode() -> None:
    """Q48: they need no external load, and a TOTAL default would make the
    validator ask for one."""
    from app.domain.training.activities import LoadMode

    for exercise in CATALOG_SEED:
        if exercise.is_bodyweight:
            assert exercise.default_load_mode in (
                LoadMode.BODYWEIGHT,
                LoadMode.BODYWEIGHT_PLUS,
                LoadMode.BODYWEIGHT_MINUS,
            ), f"{exercise.slug} is bodyweight but defaults to {exercise.default_load_mode}"
