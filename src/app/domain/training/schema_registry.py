"""What each activity type requires, as data (§14.1, Q45, Q46, Q47).

A registry rather than a chain of `if activity_type is ...`: the rules are a
table, and writing them as one makes the difference between STRENGTH and
DISTANCE_ACTIVITY reviewable in ten lines instead of spread across a function.
It is also what lets a test iterate `ActivityType` and fail when a new member
arrives without a schema — the failure mode being prevented is a new activity
type that validates everything because nobody wrote its rules.

The ranges are deliberately wide. They exist to catch input that is *broken* --
a negative rep count, a 30-hour run -- not to second-guess what somebody
achieved. A range narrow enough to be interesting would eventually refuse a
real workout, and refusing real work is worse than storing a surprising number.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from app.domain.training.activities import ActivityField, ActivityType


@dataclass(frozen=True, slots=True)
class ValueRange:
    minimum: Decimal
    maximum: Decimal

    def contains(self, value: Decimal) -> bool:
        return self.minimum <= value <= self.maximum


@dataclass(frozen=True, slots=True)
class ActivitySchema:
    activity_type: ActivityType
    #: Fields without which the activity cannot be recorded at all (Q46).
    essential: frozenset[ActivityField] = frozenset()
    optional: frozenset[ActivityField] = frozenset()
    #: Each group needs at least one member present. This is Q47's "distance
    #: and/or duration": neither field alone is essential, but a run with
    #: neither is not a run anybody can do anything with.
    at_least_one_of: tuple[frozenset[ActivityField], ...] = ()
    ranges: Mapping[ActivityField, ValueRange] = field(default_factory=dict)

    def accepts(self, activity_field: ActivityField) -> bool:
        """Whether this field means anything for this activity type.

        A distance on a bench press does not, and §14.1's "avoid invented
        values" cuts both ways: the validator warns rather than dropping it
        silently, because the input said something and the user deserves to
        know it was ignored.
        """
        if activity_field in self.essential or activity_field in self.optional:
            return True
        return any(activity_field in group for group in self.at_least_one_of)


_REPS = ValueRange(Decimal(1), Decimal(500))
_LOAD = ValueRange(Decimal(0), Decimal(1000))
_DISTANCE = ValueRange(Decimal(1), Decimal(500_000))
_DURATION = ValueRange(Decimal(1), Decimal(86_400))
_MOBILITY_DURATION = ValueRange(Decimal(1), Decimal(7_200))

_STRENGTH_RANGES: Final[Mapping[ActivityField, ValueRange]] = {
    ActivityField.REPETITIONS: _REPS,
    ActivityField.LOAD: _LOAD,
}
_ENDURANCE_RANGES: Final[Mapping[ActivityField, ValueRange]] = {
    ActivityField.DISTANCE: _DISTANCE,
    ActivityField.DURATION: _DURATION,
}


DEFAULT_ACTIVITY_SCHEMAS: Final[Mapping[ActivityType, ActivitySchema]] = {
    ActivityType.STRENGTH: ActivitySchema(
        activity_type=ActivityType.STRENGTH,
        # Q46: repetitions are what make a strength set a set. Load is not --
        # "10 flexões" is complete, and "supino 80 kg" is not.
        essential=frozenset({ActivityField.REPETITIONS}),
        optional=frozenset({ActivityField.LOAD, ActivityField.LOAD_MODE, ActivityField.EFFORT}),
        ranges=_STRENGTH_RANGES,
    ),
    ActivityType.DISTANCE_ACTIVITY: ActivitySchema(
        activity_type=ActivityType.DISTANCE_ACTIVITY,
        optional=frozenset({ActivityField.EFFORT}),
        # Q47: distance, duration, or both.
        at_least_one_of=(frozenset({ActivityField.DISTANCE, ActivityField.DURATION}),),
        ranges=_ENDURANCE_RANGES,
    ),
    ActivityType.TIMED_ACTIVITY: ActivitySchema(
        activity_type=ActivityType.TIMED_ACTIVITY,
        essential=frozenset({ActivityField.DURATION}),
        optional=frozenset({ActivityField.EFFORT, ActivityField.LOAD, ActivityField.LOAD_MODE}),
        ranges={ActivityField.DURATION: _DURATION, ActivityField.LOAD: _LOAD},
    ),
    ActivityType.MIXED_ACTIVITY: ActivitySchema(
        activity_type=ActivityType.MIXED_ACTIVITY,
        optional=frozenset({ActivityField.LOAD, ActivityField.LOAD_MODE, ActivityField.EFFORT}),
        at_least_one_of=(
            frozenset({ActivityField.REPETITIONS, ActivityField.DISTANCE, ActivityField.DURATION}),
        ),
        ranges={**_STRENGTH_RANGES, **_ENDURANCE_RANGES},
    ),
    ActivityType.MOBILITY: ActivitySchema(
        activity_type=ActivityType.MOBILITY,
        optional=frozenset({ActivityField.EFFORT}),
        at_least_one_of=(frozenset({ActivityField.DURATION, ActivityField.REPETITIONS}),),
        ranges={
            ActivityField.DURATION: _MOBILITY_DURATION,
            ActivityField.REPETITIONS: _REPS,
        },
    ),
    ActivityType.OTHER: ActivitySchema(
        activity_type=ActivityType.OTHER,
        optional=frozenset({ActivityField.LOAD_MODE, ActivityField.EFFORT}),
        # §14.1 asks for a conservative schema that avoids invented values.
        # Conservative here means *require something*, not *accept anything*:
        # an activity with no measure at all is a sentence, not a record.
        at_least_one_of=(
            frozenset(
                {
                    ActivityField.REPETITIONS,
                    ActivityField.DISTANCE,
                    ActivityField.DURATION,
                    ActivityField.LOAD,
                }
            ),
        ),
        ranges={**_STRENGTH_RANGES, **_ENDURANCE_RANGES},
    ),
}


class UnknownActivityTypeError(LookupError):
    def __init__(self, activity_type: ActivityType) -> None:
        super().__init__(f"no schema is registered for activity type {activity_type.value!r}")
        self.activity_type = activity_type


class ActivitySchemaRegistry:
    """Deterministic (§14.1): the same draft always meets the same rules."""

    def __init__(self, schemas: Mapping[ActivityType, ActivitySchema] | None = None) -> None:
        self._schemas = dict(DEFAULT_ACTIVITY_SCHEMAS if schemas is None else schemas)

    def schema_for(self, activity_type: ActivityType) -> ActivitySchema:
        try:
            return self._schemas[activity_type]
        except KeyError as error:
            # Loudly, rather than validating nothing: an activity type with no
            # schema would otherwise accept every draft it was handed.
            raise UnknownActivityTypeError(activity_type) from error

    def covered_types(self) -> frozenset[ActivityType]:
        return frozenset(self._schemas)
