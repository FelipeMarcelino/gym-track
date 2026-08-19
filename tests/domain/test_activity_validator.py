"""WS-2: the validation rules of §14.1, table-driven.

These are the rules Sprint 3's extractor will be judged against, so they are
pinned here before any model exists to argue with them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.training.activities import (
    ActivityDraft,
    ActivityField,
    ActivityType,
    LoadMode,
    SetType,
)
from app.domain.training.schema_registry import (
    ActivitySchemaRegistry,
    UnknownActivityTypeError,
    ValueRange,
)
from app.domain.training.validation import (
    ActivityValidator,
    IssueCode,
    ValidationStatus,
)


@pytest.fixture
def validator() -> ActivityValidator:
    return ActivityValidator()


def strength(**overrides: object) -> ActivityDraft:
    return ActivityDraft(activity_type=ActivityType.STRENGTH, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Strength (Q46, Q48)
# --------------------------------------------------------------------------


def test_a_strength_set_without_repetitions_asks_for_them(
    validator: ActivityValidator,
) -> None:
    """Q46: "supino 80 kg" is incomplete, and the reply must name what is
    missing rather than invent a rep count."""
    outcome = validator.validate(strength(load_kg=Decimal(80), load_mode=LoadMode.TOTAL))

    assert outcome.status is ValidationStatus.MISSING_ESSENTIAL_DATA
    assert outcome.missing_fields == (ActivityField.REPETITIONS,)
    assert not outcome.is_persistable
    assert "repetições" in outcome.issues[0].message


def test_a_strength_set_without_load_is_complete(validator: ActivityValidator) -> None:
    """Q48: "10 flexões" is a whole set. Load is optional, and demanding it
    would make every bodyweight exercise unloggable."""
    outcome = validator.validate(strength(repetitions=10))

    assert outcome.status is ValidationStatus.VALID
    assert outcome.issues == ()
    assert outcome.is_persistable


def test_a_full_strength_set_is_valid(validator: ActivityValidator) -> None:
    outcome = validator.validate(
        strength(
            repetitions=8,
            load_kg=Decimal("82.5"),
            load_mode=LoadMode.TOTAL,
            set_type=SetType.WORKING,
        )
    )

    assert outcome.status is ValidationStatus.VALID


def test_zero_load_is_accepted(validator: ActivityValidator) -> None:
    """An unloaded barbell is 20 kg, but a user reporting 0 means bodyweight —
    refusing it would make them round up to something untrue."""
    outcome = validator.validate(strength(repetitions=12, load_kg=Decimal(0)))

    assert outcome.status is ValidationStatus.VALID


# --------------------------------------------------------------------------
# Distance and duration (Q47)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        ActivityDraft(activity_type=ActivityType.DISTANCE_ACTIVITY, distance_m=Decimal(5000)),
        ActivityDraft(activity_type=ActivityType.DISTANCE_ACTIVITY, duration_s=Decimal(1800)),
        ActivityDraft(
            activity_type=ActivityType.DISTANCE_ACTIVITY,
            distance_m=Decimal(5000),
            duration_s=Decimal(1500),
        ),
    ],
    ids=["distance only", "duration only", "both"],
)
def test_a_distance_activity_accepts_distance_duration_or_both(
    validator: ActivityValidator, draft: ActivityDraft
) -> None:
    assert validator.validate(draft).status is ValidationStatus.VALID


def test_a_distance_activity_with_neither_names_both_options(
    validator: ActivityValidator,
) -> None:
    outcome = validator.validate(ActivityDraft(activity_type=ActivityType.DISTANCE_ACTIVITY))

    assert outcome.status is ValidationStatus.MISSING_ESSENTIAL_DATA
    assert set(outcome.missing_fields) == {ActivityField.DISTANCE, ActivityField.DURATION}
    assert all("ou" in issue.message for issue in outcome.issues)


def test_a_timed_activity_needs_its_duration(validator: ActivityValidator) -> None:
    outcome = validator.validate(ActivityDraft(activity_type=ActivityType.TIMED_ACTIVITY))

    assert outcome.status is ValidationStatus.MISSING_ESSENTIAL_DATA
    assert outcome.missing_fields == (ActivityField.DURATION,)


def test_mobility_accepts_duration_alone(validator: ActivityValidator) -> None:
    outcome = validator.validate(
        ActivityDraft(activity_type=ActivityType.MOBILITY, duration_s=Decimal(600))
    )

    assert outcome.status is ValidationStatus.VALID


def test_mobility_accepts_repetitions_alone(validator: ActivityValidator) -> None:
    outcome = validator.validate(ActivityDraft(activity_type=ActivityType.MOBILITY, repetitions=15))

    assert outcome.status is ValidationStatus.VALID


def test_other_still_requires_something(validator: ActivityValidator) -> None:
    """§14.1's conservative schema means *require something*, not *accept
    anything*: an activity with no measure is a sentence, not a record."""
    outcome = validator.validate(ActivityDraft(activity_type=ActivityType.OTHER))

    assert outcome.status is ValidationStatus.MISSING_ESSENTIAL_DATA


# --------------------------------------------------------------------------
# Broken input
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("draft", "expected_field", "expected_code"),
    [
        (strength(repetitions=-3), ActivityField.REPETITIONS, IssueCode.NEGATIVE),
        (
            strength(repetitions=10, load_kg=Decimal(-5)),
            ActivityField.LOAD,
            IssueCode.NEGATIVE,
        ),
        (strength(repetitions=10_000), ActivityField.REPETITIONS, IssueCode.OUT_OF_RANGE),
        (
            strength(repetitions=10, load_kg=Decimal(5000)),
            ActivityField.LOAD,
            IssueCode.OUT_OF_RANGE,
        ),
        (
            ActivityDraft(
                activity_type=ActivityType.DISTANCE_ACTIVITY, duration_s=Decimal(200_000)
            ),
            ActivityField.DURATION,
            IssueCode.OUT_OF_RANGE,
        ),
    ],
    ids=["negative reps", "negative load", "absurd reps", "absurd load", "30-hour run"],
)
def test_broken_values_are_invalid_not_incomplete(
    validator: ActivityValidator,
    draft: ActivityDraft,
    expected_field: ActivityField,
    expected_code: IssueCode,
) -> None:
    outcome = validator.validate(draft)

    assert outcome.status is ValidationStatus.INVALID
    assert not outcome.is_persistable
    assert any(
        issue.field is expected_field and issue.code is expected_code for issue in outcome.issues
    )


def test_zero_repetitions_are_invalid(validator: ActivityValidator) -> None:
    """A zero-rep set is not an incomplete set: there is nothing the user can
    add that makes it one."""
    outcome = validator.validate(strength(repetitions=0))

    assert outcome.status is ValidationStatus.INVALID


def test_a_broken_value_outranks_a_missing_one(validator: ActivityValidator) -> None:
    """Asking for a load the user never mentioned, while ignoring the
    impossible rep count they did, would be the wrong reply."""
    outcome = validator.validate(ActivityDraft(activity_type=ActivityType.STRENGTH, repetitions=-3))

    assert outcome.status is ValidationStatus.INVALID
    assert outcome.missing_fields == ()


# --------------------------------------------------------------------------
# Warnings
# --------------------------------------------------------------------------


def test_a_field_that_does_not_apply_is_warned_about_not_dropped(
    validator: ActivityValidator,
) -> None:
    """A distance on a bench press is nonsense, but the input said it. A value
    that disappears without a trace is how a user learns not to trust what the
    system recorded."""
    outcome = validator.validate(strength(repetitions=10, distance_m=Decimal(100)))

    assert outcome.status is ValidationStatus.VALID_WITH_WARNINGS
    assert outcome.is_persistable, "the set is still real work"
    assert outcome.issues[0].field is ActivityField.DISTANCE
    assert outcome.issues[0].code is IssueCode.UNEXPECTED


def test_warnings_do_not_hide_missing_essentials(validator: ActivityValidator) -> None:
    outcome = validator.validate(
        ActivityDraft(activity_type=ActivityType.STRENGTH, distance_m=Decimal(100))
    )

    assert outcome.status is ValidationStatus.MISSING_ESSENTIAL_DATA


# --------------------------------------------------------------------------
# The registry itself
# --------------------------------------------------------------------------


def test_the_registry_covers_every_activity_type() -> None:
    """A new ActivityType without a schema would validate everything it was
    handed. This test is the thing that stops that from shipping."""
    assert ActivitySchemaRegistry().covered_types() == frozenset(ActivityType)


@pytest.mark.parametrize("activity_type", list(ActivityType))
def test_every_schema_can_reject_something(activity_type: ActivityType) -> None:
    """A schema with no essentials and no groups accepts an empty draft, which
    means that activity type records nothing at all."""
    schema = ActivitySchemaRegistry().schema_for(activity_type)

    assert schema.essential or schema.at_least_one_of


def test_an_unregistered_type_fails_loudly() -> None:
    registry = ActivitySchemaRegistry(schemas={})

    with pytest.raises(UnknownActivityTypeError):
        registry.schema_for(ActivityType.STRENGTH)


def test_every_issue_names_a_field_and_says_something() -> None:
    """An issue without a field cannot be turned into a question, and one
    without a message cannot be shown."""
    validator = ActivityValidator()
    drafts = [
        strength(),
        strength(repetitions=-1),
        strength(repetitions=10, distance_m=Decimal(10)),
        ActivityDraft(activity_type=ActivityType.DISTANCE_ACTIVITY),
        ActivityDraft(activity_type=ActivityType.OTHER),
    ]

    for draft in drafts:
        for issue in validator.validate(draft).issues:
            assert isinstance(issue.field, ActivityField)
            assert issue.message.strip()


def test_a_custom_registry_is_honoured() -> None:
    """The registry is injectable so a later sprint can tighten a range without
    editing the validator."""
    from app.domain.training.schema_registry import ActivitySchema

    registry = ActivitySchemaRegistry(
        schemas={
            ActivityType.STRENGTH: ActivitySchema(
                activity_type=ActivityType.STRENGTH,
                essential=frozenset({ActivityField.REPETITIONS}),
                ranges={ActivityField.REPETITIONS: ValueRange(Decimal(1), Decimal(5))},
            )
        }
    )

    outcome = ActivityValidator(registry).validate(strength(repetitions=10))

    assert outcome.status is ValidationStatus.INVALID


def test_the_draft_reports_only_the_fields_that_were_stated() -> None:
    """`None` means "not stated" everywhere, so a validator can tell an absent
    load from a zero one."""
    draft = strength(repetitions=5, load_kg=Decimal(0))

    assert draft.stated_fields() == frozenset({ActivityField.REPETITIONS, ActivityField.LOAD})
    assert strength().stated_fields() == frozenset()
