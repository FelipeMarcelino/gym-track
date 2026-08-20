"""The catalog as the resolver needs to see it (Q155).

Four lookups and a listing, which is exactly §16's stages and nothing else. The
resolver never learns that the catalog is PostgreSQL, so the staged logic is
testable against an in-memory catalog without a container — and the ordering of
the stages, which is the part that is normative, is tested without a database
being able to hide a mistake in it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.domain.training.activities import ActivityType, LoadMode


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    exercise_id: UUID
    canonical_name: str
    activity_type: ActivityType
    default_load_mode: LoadMode
    is_bodyweight: bool
    #: Whether this exercise is performed with implements held per side, from
    #: `equipment.is_implement`. Q49's "60kg de halteres" question is decided
    #: from this column rather than from pattern-matching the name.
    uses_implements: bool


@dataclass(frozen=True, slots=True)
class SearchableExercise:
    entry: CatalogEntry
    #: The canonical name and every alias, already normalized. Fuzzy matching
    #: scores against all of them, because "rdl" and "levantamento terra
    #: romeno" are the same exercise reached by very different strings.
    normalized_terms: tuple[str, ...]


class ExerciseCatalogPort(Protocol):
    async def by_user_alias(self, normalized: str, user_id: UUID) -> CatalogEntry | None: ...

    async def by_global_alias(self, normalized: str) -> CatalogEntry | None: ...

    async def by_canonical_name(self, normalized: str) -> CatalogEntry | None: ...

    async def all_searchable(self) -> Sequence[SearchableExercise]: ...
