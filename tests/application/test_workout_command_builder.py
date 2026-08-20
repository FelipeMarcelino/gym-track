"""WS-8: turning structured input into something committable (Q56, Q57, Q49).

The builder is where every earlier workstream meets: WS-7 resolves the name,
WS-8's inheritance fills the gaps the user left, WS-3 parses the units, WS-4
reads the effort, WS-2 says whether the result is worth writing.

Its governing rule is Q57: an activity nobody could understand must not cost
the user the ones we did. Every failure is scoped to the activity that caused
it, and the rest of the workout still commits.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.application.commands.workout import (
    DeferralReason,
    EmptyCommandError,
    LogWorkoutCommand,
    operation_id_for,
)
from app.application.services.exercise_resolver import ExerciseResolver
from app.application.services.workout_command_builder import WorkoutCommandBuilder
from app.domain.training.activities import ActivityField, ActivityType, LoadMode
from app.domain.training.effort import EffortMethod, EffortNormalizer
from app.domain.training.input_contract import (
    StructuredActivityInput,
    StructuredGroupInput,
    StructuredSetInput,
    StructuredWorkoutInput,
)
from app.domain.training.metrics import METRIC_VERSIONS
from app.domain.training.provenance import ExerciseGroupType, Provenance
from app.domain.training.validation import ActivityValidator
from tests.application.test_exercise_resolver import FakeCatalog

USER = uuid4()
CONVERSATION = uuid4()
BATCH = uuid4()
MESSAGES = (uuid4(),)


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog()


@pytest.fixture
def builder(catalog: FakeCatalog) -> WorkoutCommandBuilder:
    return WorkoutCommandBuilder(
        resolver=ExerciseResolver(catalog),
        validator=ActivityValidator(),
        effort=EffortNormalizer(),
    )


def _input(*activities: StructuredActivityInput, **kwargs: object) -> StructuredWorkoutInput:
    return StructuredWorkoutInput(activities=activities, **kwargs)  # type: ignore[arg-type]


async def _build(builder: WorkoutCommandBuilder, structured: StructuredWorkoutInput) -> object:
    return await builder.build(
        structured,
        user_id=USER,
        conversation_id=CONVERSATION,
        message_batch_id=BATCH,
        source_message_ids=MESSAGES,
    )


# --------------------------------------------------------------------------
# The happy path, and what it carries
# --------------------------------------------------------------------------


async def test_a_stated_load_reaches_the_command_in_kilograms(
    builder: WorkoutCommandBuilder,
) -> None:
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino",
                sets=(StructuredSetInput(repetitions=10, load="80kg"),),
            )
        ),
    )

    assert outcome.command is not None  # type: ignore[attr-defined]
    activity = outcome.command.activities[0]  # type: ignore[attr-defined]
    assert activity.canonical_name == "Supino reto"
    assert activity.sets[0].load_kg == Decimal("80")
    assert activity.sets[0].raw_load_text == "80kg"
    assert activity.sets[0].repetitions == 10


async def test_pounds_are_converted_rather_than_stored_as_stated(
    builder: WorkoutCommandBuilder,
) -> None:
    """D4: one unit in the database. The raw text travels alongside so the
    user's own words survive the conversion (§19)."""
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino", sets=(StructuredSetInput(repetitions=5, load="176lb"),)
            )
        ),
    )

    a_set = outcome.command.activities[0].sets[0]  # type: ignore[attr-defined]
    assert a_set.load_kg is not None
    assert Decimal("79.8") < a_set.load_kg < Decimal("80.0")
    assert a_set.raw_load_text == "176lb"


async def test_a_carried_load_is_marked_as_carried(builder: WorkoutCommandBuilder) -> None:
    """§14.4 end to end: the command records which numbers the user stated."""
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino",
                sets=(
                    StructuredSetInput(repetitions=10, load="80kg"),
                    StructuredSetInput(repetitions=9),
                ),
            )
        ),
    )

    sets = outcome.command.activities[0].sets  # type: ignore[attr-defined]
    assert [item.load_provenance for item in sets] == [Provenance.EXPLICIT, Provenance.INHERITED]
    assert [item.repetitions_provenance for item in sets] == [
        Provenance.EXPLICIT,
        Provenance.EXPLICIT,
    ]
    assert [item.set_index for item in sets] == [0, 1]


async def test_every_derived_number_carries_a_version_we_know(
    builder: WorkoutCommandBuilder,
) -> None:
    """Q52, at the point the number is produced rather than at the column."""
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino", sets=(StructuredSetInput(repetitions=10, load="80kg"),)
            )
        ),
    )

    metrics = outcome.command.activities[0].sets[0].metrics  # type: ignore[attr-defined]
    assert metrics
    assert {metric.version for metric in metrics} <= set(METRIC_VERSIONS.values())


# --------------------------------------------------------------------------
# What the catalog decides (Q49)
# --------------------------------------------------------------------------


async def test_a_dumbbell_exercise_is_per_implement_without_being_told(
    builder: WorkoutCommandBuilder,
) -> None:
    """Q49: "20kg de halteres" is 20 per hand. The answer comes from the
    equipment row, and the input never mentions a mode."""
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino com halteres",
                sets=(StructuredSetInput(repetitions=10, load="20kg"),),
            )
        ),
    )

    assert outcome.command.activities[0].sets[0].load_mode is LoadMode.PER_IMPLEMENT  # type: ignore[attr-defined]


async def test_a_bodyweight_exercise_has_no_load_and_says_so(
    builder: WorkoutCommandBuilder,
) -> None:
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="barra fixa", sets=(StructuredSetInput(repetitions=8),)
            )
        ),
    )

    a_set = outcome.command.activities[0].sets[0]  # type: ignore[attr-defined]
    assert a_set.load_mode is LoadMode.BODYWEIGHT
    assert a_set.load_kg is None


async def test_a_stated_mode_beats_the_catalogs_default(builder: WorkoutCommandBuilder) -> None:
    """The catalog is the fallback, not an override: a user who says "total"
    has said something the catalog cannot know."""
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino com halteres",
                sets=(StructuredSetInput(repetitions=10, load="40kg", load_mode=LoadMode.TOTAL),),
            )
        ),
    )

    assert outcome.command.activities[0].sets[0].load_mode is LoadMode.TOTAL  # type: ignore[attr-defined]


async def test_the_activity_type_comes_from_the_catalog_when_unstated(
    builder: WorkoutCommandBuilder,
) -> None:
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="corrida",
                sets=(StructuredSetInput(distance="5km", duration="25:00"),),
            )
        ),
    )

    assert outcome.command.activities[0].activity_type is ActivityType.DISTANCE_ACTIVITY  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Effort (§14.3)
# --------------------------------------------------------------------------


async def test_activity_effort_stays_at_the_activity_level(
    builder: WorkoutCommandBuilder,
) -> None:
    """WS-4's rule, now wired: an effort reported for the exercise is not a
    claim about each set."""
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino",
                effort="RPE 9",
                sets=(
                    StructuredSetInput(repetitions=10, load="80kg"),
                    StructuredSetInput(repetitions=9),
                ),
            )
        ),
    )

    activity = outcome.command.activities[0]  # type: ignore[attr-defined]
    assert activity.effort is not None
    assert activity.effort.rpe == Decimal("9")
    assert all(item.effort is None for item in activity.sets)


async def test_an_unreadable_effort_is_kept_rather_than_dropped(
    builder: WorkoutCommandBuilder,
) -> None:
    """ "No effort reported" and "an effort we could not read" are different
    facts, and only one of them is worth asking about."""
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino",
                sets=(StructuredSetInput(repetitions=10, load="80kg", effort="foi de boa"),),
            )
        ),
    )

    effort = outcome.command.activities[0].sets[0].effort  # type: ignore[attr-defined]
    assert effort is not None
    assert effort.raw == "foi de boa"
    assert effort.method is EffortMethod.UNNORMALIZED


# --------------------------------------------------------------------------
# Q56 and Q57 — what gets deferred, and what survives it
# --------------------------------------------------------------------------


async def test_one_unresolvable_activity_does_not_cost_the_others(
    builder: WorkoutCommandBuilder,
) -> None:
    """Q57. The user did the bench press, and losing it because we could not
    understand the second sentence is the failure this rule exists to stop."""
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino", sets=(StructuredSetInput(repetitions=10, load="80kg"),)
            ),
            StructuredActivityInput(
                raw_name="aquele exercício do peito", sets=(StructuredSetInput(repetitions=10),)
            ),
        ),
    )

    assert len(outcome.command.activities) == 1  # type: ignore[attr-defined]
    assert outcome.command.activities[0].canonical_name == "Supino reto"  # type: ignore[attr-defined]
    assert [item.reason for item in outcome.deferred] == [  # type: ignore[attr-defined]
        DeferralReason.UNRESOLVED_EXERCISE
    ]
    assert outcome.deferred[0].raw_name == "aquele exercício do peito"  # type: ignore[attr-defined]


async def test_an_ambiguous_name_is_deferred_with_its_candidates(
    builder: WorkoutCommandBuilder,
) -> None:
    """Q56: the caller needs the candidates to ask a useful question."""
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino clinado", sets=(StructuredSetInput(repetitions=10, load="60kg"),)
            )
        ),
    )

    assert outcome.command is None  # type: ignore[attr-defined]
    deferred = outcome.deferred[0]  # type: ignore[attr-defined]
    assert deferred.reason is DeferralReason.AMBIGUOUS_EXERCISE
    assert len(deferred.candidates) >= 2


async def test_a_strength_set_without_reps_is_deferred_naming_the_field(
    builder: WorkoutCommandBuilder,
) -> None:
    """Q46: the reply has to say what is missing, so the field travels with the
    deferral rather than being re-derived by whoever writes the message."""
    outcome = await _build(
        builder,
        _input(StructuredActivityInput(raw_name="supino", sets=(StructuredSetInput(load="80kg"),))),
    )

    assert outcome.command is None  # type: ignore[attr-defined]
    deferred = outcome.deferred[0]  # type: ignore[attr-defined]
    assert deferred.reason is DeferralReason.MISSING_ESSENTIAL_DATA
    assert deferred.missing_field is ActivityField.REPETITIONS


async def test_a_load_nobody_can_parse_is_deferred_rather_than_guessed(
    builder: WorkoutCommandBuilder,
) -> None:
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino", sets=(StructuredSetInput(repetitions=10, load="umas pesadas"),)
            )
        ),
    )

    assert outcome.command is None  # type: ignore[attr-defined]
    assert outcome.deferred[0].reason is DeferralReason.INVALID_VALUE  # type: ignore[attr-defined]


async def test_nothing_committable_produces_no_command_at_all(
    builder: WorkoutCommandBuilder,
) -> None:
    outcome = await _build(
        builder,
        _input(StructuredActivityInput(raw_name="treino de ontem", sets=(StructuredSetInput(),))),
    )

    assert outcome.command is None  # type: ignore[attr-defined]
    assert outcome.deferred  # type: ignore[attr-defined]


def test_a_command_with_nothing_in_it_cannot_be_constructed() -> None:
    """An empty command would commit a session, an audit row and a domain event
    describing work that does not exist."""
    with pytest.raises(EmptyCommandError):
        LogWorkoutCommand(
            operation_id=operation_id_for(BATCH),
            user_id=USER,
            conversation_id=CONVERSATION,
            message_batch_id=BATCH,
            source_message_ids=MESSAGES,
            activities=(),
        )


# --------------------------------------------------------------------------
# Groups (Q51)
# --------------------------------------------------------------------------


async def test_an_activity_with_no_sets_is_deferred_rather_than_written(
    builder: WorkoutCommandBuilder,
) -> None:
    """ "Fiz supino hoje" is a real thing to say and not a workout yet. A block
    with no sets records that an exercise happened and nothing about it, which
    is worse than asking."""
    outcome = await _build(builder, _input(StructuredActivityInput(raw_name="supino")))

    assert outcome.command is None  # type: ignore[attr-defined]
    assert outcome.deferred[0].reason is DeferralReason.MISSING_ESSENTIAL_DATA  # type: ignore[attr-defined]


async def test_a_command_only_ever_carries_the_key_the_batch_implies() -> None:
    """The invariant WS-9 leans on, asserted at the type rather than trusted."""
    assert operation_id_for(BATCH) == f"log_workout:{BATCH}"


async def test_a_superset_reaches_the_command_with_its_members(
    builder: WorkoutCommandBuilder,
) -> None:
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino",
                group_ref="A",
                sets=(StructuredSetInput(repetitions=10, load="80kg"),),
            ),
            StructuredActivityInput(
                raw_name="remada curvada",
                group_ref="A",
                sets=(StructuredSetInput(repetitions=10, load="60kg"),),
            ),
            groups=(
                StructuredGroupInput(ref="A", group_type=ExerciseGroupType.SUPERSET, rounds=3),
            ),
        ),
    )

    command = outcome.command  # type: ignore[attr-defined]
    assert [group.ref for group in command.groups] == ["A"]
    assert command.groups[0].group_type is ExerciseGroupType.SUPERSET
    assert command.groups[0].rounds == 3
    assert {activity.group_ref for activity in command.activities} == {"A"}


async def test_a_group_nobody_is_left_in_is_dropped(builder: WorkoutCommandBuilder) -> None:
    """If every member of a superset was deferred, the group describes nothing.
    Writing it would leave a superset of zero exercises in the history."""
    outcome = await _build(
        builder,
        _input(
            StructuredActivityInput(
                raw_name="supino", sets=(StructuredSetInput(repetitions=10, load="80kg"),)
            ),
            StructuredActivityInput(
                raw_name="treino de ontem", group_ref="A", sets=(StructuredSetInput(),)
            ),
            groups=(StructuredGroupInput(ref="A", group_type=ExerciseGroupType.CIRCUIT),),
        ),
    )

    assert outcome.command is not None  # type: ignore[attr-defined]
    assert outcome.command.groups == ()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Idempotency (WS-9 depends on this)
# --------------------------------------------------------------------------


async def test_the_operation_id_is_derived_from_the_batch(
    builder: WorkoutCommandBuilder,
) -> None:
    """A redelivery has to compute the same key without carrying state, or
    at-least-once delivery writes the workout twice (DEC-005). The builder
    computes it rather than accepting one: a caller that passed a fresh value
    for the same batch would bypass `processed_operations` entirely, and
    nothing downstream could tell."""
    structured = _input(
        StructuredActivityInput(
            raw_name="supino", sets=(StructuredSetInput(repetitions=10, load="80kg"),)
        )
    )

    first = await _build(builder, structured)
    second = await _build(builder, structured)

    assert first.command.operation_id == second.command.operation_id  # type: ignore[attr-defined]
    assert first.command.operation_id == f"log_workout:{BATCH}"  # type: ignore[attr-defined]
