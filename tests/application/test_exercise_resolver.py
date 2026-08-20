"""WS-7: resolving what the user called an exercise into a catalog row (§16).

The resolver is deterministic this sprint — four stages, first hit wins, no
model in the loop. The tests that matter most are the ones asserting an
*absence*: the sprint file calls resolving to the wrong exercise the worst
failure available to this system, because a wrong row is silently wrong forever
while an unresolved one asks a question.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.application.ports.exercise_catalog import CatalogEntry, SearchableExercise
from app.application.services.exercise_resolver import ExerciseResolver
from app.domain.exercises.catalog_data import CATALOG_SEED
from app.domain.exercises.normalization import normalize_for_match
from app.domain.exercises.resolution import (
    UNIMPLEMENTED_METHODS,
    ExerciseResolution,
    ResolutionMethod,
)
from app.domain.training.activities import ActivityType, LoadMode

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "domain" / "fixtures" / "exercise_resolution.json"
    ).read_text(encoding="utf-8")
)
GOLDEN_ROWS = FIXTURE["rows"]

USER = uuid4()
OTHER_USER = uuid4()


class FakeCatalog:
    """The seeded catalog, in memory, plus whatever a test adds to it."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, CatalogEntry] = {}
        self._global_aliases: dict[str, UUID] = {}
        self._user_aliases: dict[tuple[UUID, str], UUID] = {}
        self._canonical: dict[str, UUID] = {}
        self._terms: dict[UUID, list[str]] = {}

        for seed in CATALOG_SEED:
            entry = CatalogEntry(
                exercise_id=uuid4(),
                canonical_name=seed.canonical_name,
                activity_type=seed.activity_type,
                default_load_mode=seed.default_load_mode,
                is_bodyweight=seed.is_bodyweight,
                uses_implements="halteres" in seed.equipment,
            )
            self._by_id[entry.exercise_id] = entry
            self._canonical[normalize_for_match(seed.canonical_name)] = entry.exercise_id
            self._terms[entry.exercise_id] = [normalize_for_match(seed.canonical_name)]
            for alias in seed.aliases_pt_br + seed.aliases_en:
                normalized = normalize_for_match(alias)
                self._global_aliases.setdefault(normalized, entry.exercise_id)
                self._terms[entry.exercise_id].append(normalized)

    def id_of(self, canonical_name: str) -> UUID:
        return self._canonical[normalize_for_match(canonical_name)]

    def add_user_alias(self, *, user_id: UUID, alias: str, canonical_name: str) -> None:
        self._user_aliases[(user_id, normalize_for_match(alias))] = self.id_of(canonical_name)

    def add_exercise(self, canonical_name: str, *, aliases: Sequence[str] = ()) -> UUID:
        entry = CatalogEntry(
            exercise_id=uuid4(),
            canonical_name=canonical_name,
            activity_type=ActivityType.STRENGTH,
            default_load_mode=LoadMode.TOTAL,
            is_bodyweight=False,
            uses_implements=False,
        )
        self._by_id[entry.exercise_id] = entry
        self._canonical[normalize_for_match(canonical_name)] = entry.exercise_id
        self._terms[entry.exercise_id] = [normalize_for_match(canonical_name)]
        for alias in aliases:
            normalized = normalize_for_match(alias)
            self._global_aliases.setdefault(normalized, entry.exercise_id)
            self._terms[entry.exercise_id].append(normalized)
        return entry.exercise_id

    async def by_user_alias(self, normalized: str, user_id: UUID) -> CatalogEntry | None:
        found = self._user_aliases.get((user_id, normalized))
        return self._by_id[found] if found else None

    async def by_global_alias(self, normalized: str) -> CatalogEntry | None:
        found = self._global_aliases.get(normalized)
        return self._by_id[found] if found else None

    async def by_canonical_name(self, normalized: str) -> CatalogEntry | None:
        found = self._canonical.get(normalized)
        return self._by_id[found] if found else None

    async def all_searchable(self) -> Sequence[SearchableExercise]:
        return [
            SearchableExercise(entry=entry, normalized_terms=tuple(self._terms[exercise_id]))
            for exercise_id, entry in self._by_id.items()
        ]


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog()


@pytest.fixture
def resolver(catalog: FakeCatalog) -> ExerciseResolver:
    return ExerciseResolver(catalog)


# --------------------------------------------------------------------------
# The golden table
# --------------------------------------------------------------------------


@pytest.mark.parametrize("row", GOLDEN_ROWS, ids=[row["raw"] or "<empty>" for row in GOLDEN_ROWS])
async def test_the_golden_table_of_real_input(
    resolver: ExerciseResolver, row: dict[str, str | None]
) -> None:
    """Frozen behaviour against names people actually type. The scorer is
    pinned in uv.lock, so a row that moves is a behaviour change and has to be
    argued for rather than absorbed."""
    resolution = await resolver.resolve(str(row["raw"]), user_id=USER)

    assert resolution.canonical_name == row["canonical"]
    assert (resolution.method.value if resolution.method else None) == row["method"]


async def test_no_row_is_resolved_by_a_stage_this_sprint_does_not_have(
    resolver: ExerciseResolver,
) -> None:
    """VECTOR, LLM and USER_CONFIRMED are declared so the vocabulary is stable
    across sprints. Returning one would mean a stage nobody wrote ran."""
    for row in GOLDEN_ROWS:
        resolution = await resolver.resolve(str(row["raw"]), user_id=USER)
        assert resolution.method not in UNIMPLEMENTED_METHODS


# --------------------------------------------------------------------------
# Stage order (§16)
# --------------------------------------------------------------------------


async def test_a_users_own_alias_wins_over_the_global_one(
    catalog: FakeCatalog, resolver: ExerciseResolver
) -> None:
    """§16's order is normative: "supino" means what this user has always meant
    by it, even when everyone else means something else."""
    catalog.add_user_alias(user_id=USER, alias="supino", canonical_name="Supino inclinado")

    mine = await resolver.resolve("supino", user_id=USER)
    theirs = await resolver.resolve("supino", user_id=OTHER_USER)

    assert mine.canonical_name == "Supino inclinado"
    assert mine.method is ResolutionMethod.USER_ALIAS
    assert theirs.canonical_name == "Supino reto"
    assert theirs.method is ResolutionMethod.GLOBAL_ALIAS


async def test_the_canonical_name_resolves_when_no_alias_covers_it(
    catalog: FakeCatalog, resolver: ExerciseResolver
) -> None:
    """Stage 3 exists for exercises whose canonical name nobody aliased."""
    catalog.add_exercise("Puxada supinada")

    resolution = await resolver.resolve("puxada supinada", user_id=USER)

    assert resolution.canonical_name == "Puxada supinada"
    assert resolution.method is ResolutionMethod.CANONICAL
    assert resolution.confidence == 1.0


# --------------------------------------------------------------------------
# What the resolver refuses to do (D3)
# --------------------------------------------------------------------------


async def test_a_weak_match_resolves_to_nothing_rather_than_to_something(
    resolver: ExerciseResolver,
) -> None:
    """The worst available failure is a confident wrong exercise: it is silently
    wrong forever, while an unresolved one asks a question. So the assertion is
    the absence, not a substitute."""
    resolution = await resolver.resolve("aquele exercício do peito", user_id=USER)

    assert resolution.exercise_id is None
    assert resolution.requires_clarification is True
    assert resolution.candidates == ()


async def test_two_close_matches_ask_instead_of_picking(resolver: ExerciseResolver) -> None:
    """Within the margin there is no best answer, and choosing the first one is
    picking by insertion order dressed up as a decision."""
    # A dropped letter leaves the word ambiguous between two real exercises
    # that are in the seed, which is the shape this actually takes in practice.
    resolution = await resolver.resolve("supino clinado", user_id=USER)

    assert resolution.exercise_id is None
    assert resolution.requires_clarification is True
    assert len(resolution.candidates) >= 2
    scores = [candidate.score for candidate in resolution.candidates]
    assert scores == sorted(scores, reverse=True), "candidates come back best-first"


async def test_a_middling_match_offers_candidates_without_demanding_an_answer(
    resolver: ExerciseResolver,
) -> None:
    """0.70-0.90 is "probably this, not sure enough to write it". Sprint 3's
    LLM stage takes this band; until then the caller may ask, and is not told
    it must."""
    # A real exercise the catalog does not have, close to one it does. The
    # Bulgarian split squat is not the back squat, and offering it is right
    # where writing it would not be.
    resolution = await resolver.resolve("agachamento bulgaro", user_id=USER)

    assert resolution.exercise_id is None
    assert resolution.requires_clarification is False
    assert resolution.candidates != ()


async def test_confidence_is_total_for_an_exact_hit(resolver: ExerciseResolver) -> None:
    resolution = await resolver.resolve("supino", user_id=USER)

    assert resolution.confidence == 1.0


# --------------------------------------------------------------------------
# The result type refuses to describe an impossible outcome
# --------------------------------------------------------------------------


def test_a_resolution_cannot_both_answer_and_ask() -> None:
    with pytest.raises(ValueError, match="clarification"):
        ExerciseResolution(
            raw_name="supino",
            exercise_id=uuid4(),
            canonical_name="Supino reto",
            method=ResolutionMethod.GLOBAL_ALIAS,
            confidence=1.0,
            requires_clarification=True,
        )


def test_a_resolution_cannot_name_an_exercise_without_saying_how() -> None:
    """A row with no method is a number nobody can audit later."""
    with pytest.raises(ValueError, match="method"):
        ExerciseResolution(
            raw_name="supino",
            exercise_id=uuid4(),
            canonical_name="Supino reto",
            confidence=1.0,
        )
