"""The seed catalog's types (§16).

Pure data, no SQLAlchemy: the curated catalog is a domain artefact that happens
to be loaded into a database, and keeping it that way means it can be validated
by a test that needs no infrastructure at all.

Slugs are the join key throughout. A seed that referred to muscles by name
would break the first time someone fixed a typo in a display name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.training.activities import ActivityType, LoadMode


class MuscleRole(StrEnum):
    """How an exercise involves a muscle (Q43)."""

    PRIMARY = "primary"
    SECONDARY = "secondary"
    STABILIZER = "stabilizer"


class ExerciseRelationType(StrEnum):
    """How two exercises relate (Q44).

    Directional: `VARIATION_OF` reads from the variation to the base movement,
    and a substitution suggestion later depends on that direction being
    consistent.
    """

    VARIATION_OF = "variation_of"
    SUBSTITUTE_FOR = "substitute_for"
    SIMILAR_MOVEMENT = "similar_movement"
    PROGRESSION_OF = "progression_of"
    REGRESSION_OF = "regression_of"


class AliasSource(StrEnum):
    """Where an alias came from.

    `SEED` aliases ship with the catalog; `USER_CONFIRMED` ones are learned in
    Sprint 3 when a user answers a clarification. The distinction matters
    because a learned alias belongs to one user and a seeded one to everybody.
    """

    SEED = "seed"
    USER_CONFIRMED = "user_confirmed"


@dataclass(frozen=True, slots=True)
class SeedMuscle:
    slug: str
    name: str
    group: str


@dataclass(frozen=True, slots=True)
class SeedEquipment:
    slug: str
    name: str
    #: True when the equipment is held one per hand, which is what makes
    #: PER_IMPLEMENT load mechanical rather than a guess about the name (Q49).
    is_implement: bool


@dataclass(frozen=True, slots=True)
class SeedExercise:
    slug: str
    canonical_name: str
    activity_type: ActivityType
    default_load_mode: LoadMode
    is_bodyweight: bool
    #: Muscle slugs. Every exercise needs at least one primary muscle, or
    #: analysis in a later sprint has nothing to group by.
    primary_muscles: tuple[str, ...]
    secondary_muscles: tuple[str, ...] = ()
    stabilizer_muscles: tuple[str, ...] = ()
    equipment: tuple[str, ...] = ()
    aliases_pt_br: tuple[str, ...] = ()
    aliases_en: tuple[str, ...] = ()

    @property
    def aliases(self) -> tuple[str, ...]:
        return self.aliases_pt_br + self.aliases_en


@dataclass(frozen=True, slots=True)
class SeedRelation:
    from_slug: str
    to_slug: str
    relation_type: ExerciseRelationType
