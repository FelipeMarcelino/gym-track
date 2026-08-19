"""The activity vocabulary and the shape a set takes before it is a command
(§14.1, §14.2, Q45, Q49, Q50).

WS-1 needed two of these enums to type the catalog's columns; the rest of the
activity model lands here. The direction matters: `infrastructure/postgres`
imports from `domain`, never the reverse, and putting the vocabulary in the
schema module would invert that the first time a validator needed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class ActivityType(StrEnum):
    """What kind of thing was performed (Q45).

    Declared in full from the MVP because the *schema* has to accommodate all
    of them; how each is validated is WS-2's concern.
    """

    STRENGTH = "strength"
    DISTANCE_ACTIVITY = "distance_activity"
    TIMED_ACTIVITY = "timed_activity"
    MIXED_ACTIVITY = "mixed_activity"
    MOBILITY = "mobility"
    OTHER = "other"


class LoadMode(StrEnum):
    """How a reported load should be read (§14.2, Q49).

    A dumbbell exercise defaults to PER_IMPLEMENT: "20 kg" on a dumbbell curl
    means 20 kg in each hand, and reading it as a total would halve every
    volume calculation built on it later.
    """

    TOTAL = "total"
    PER_SIDE = "per_side"
    PER_IMPLEMENT = "per_implement"
    BODYWEIGHT = "bodyweight"
    BODYWEIGHT_PLUS = "bodyweight_plus"
    BODYWEIGHT_MINUS = "bodyweight_minus"


class SetType(StrEnum):
    """The role a set plays (Q50).

    Explicit rather than encoded in a note: a warm-up counted as working volume
    inflates every trend built on it, and §19's analysis has no way to tell the
    difference after the fact.
    """

    WARMUP = "warmup"
    WORKING = "working"
    BACKOFF = "backoff"
    DROP_SET = "drop_set"
    FAILURE = "failure"
    AMRAP = "amrap"
    OTHER = "other"


class ActivityField(StrEnum):
    """Named so a validation outcome can point at a field rather than describe it.

    Q46 requires the system to ask for *repetitions* by name; a message saying
    "something is missing" makes the user guess what.
    """

    REPETITIONS = "repetitions"
    LOAD = "load"
    LOAD_MODE = "load_mode"
    DISTANCE = "distance"
    DURATION = "duration"
    EFFORT = "effort"


@dataclass(frozen=True, slots=True)
class ActivityDraft:
    """One set or activity in canonical units, before it becomes a command.

    Every measure is `Decimal | None`, and `None` means *not stated* -- never
    zero, never a default. Deciding whether an absence is fatal, a warning or
    perfectly fine is the validator's entire job, and it cannot do that if the
    parser has already invented a value (§14.1).

    `Decimal` rather than `float` (D4): loads and distances are added up and
    compared, and binary floating point turns "80 kg" into something that is
    not quite 80 the moment it is summed.
    """

    activity_type: ActivityType
    repetitions: int | None = None
    load_kg: Decimal | None = None
    load_mode: LoadMode | None = None
    distance_m: Decimal | None = None
    duration_s: Decimal | None = None
    #: Normalized effort on the RPE scale (§14.3). WS-4's EffortNormalizer
    #: fills this; the raw phrase the user typed travels with the persisted row
    #: rather than here, because the draft is what gets validated and an
    #: unnormalized phrase has nothing to validate.
    effort_rpe: Decimal | None = None
    set_type: SetType = SetType.WORKING

    def stated_fields(self) -> frozenset[ActivityField]:
        """Which measures the input actually carried."""
        present: set[ActivityField] = set()
        if self.repetitions is not None:
            present.add(ActivityField.REPETITIONS)
        if self.load_kg is not None:
            present.add(ActivityField.LOAD)
        if self.load_mode is not None:
            present.add(ActivityField.LOAD_MODE)
        if self.distance_m is not None:
            present.add(ActivityField.DISTANCE)
        if self.duration_s is not None:
            present.add(ActivityField.DURATION)
        if self.effort_rpe is not None:
            present.add(ActivityField.EFFORT)
        return frozenset(present)

    def value_of(self, field: ActivityField) -> Decimal | None:
        """The numeric value of a measure, for range checking."""
        match field:
            case ActivityField.REPETITIONS:
                return None if self.repetitions is None else Decimal(self.repetitions)
            case ActivityField.LOAD:
                return self.load_kg
            case ActivityField.DISTANCE:
                return self.distance_m
            case ActivityField.DURATION:
                return self.duration_s
            case ActivityField.EFFORT:
                return self.effort_rpe
            case _:
                return None
