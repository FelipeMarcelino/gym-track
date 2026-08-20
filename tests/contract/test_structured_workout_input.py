"""WS-8: the input contract, frozen (§14, D5).

Sprint 3's extractor will be written against this file and nothing else, so the
fixture is the specification: it has to parse, survive a round trip unchanged,
and cover every shape a producer needs to know how to emit.

The negative cases matter as much. `extra="forbid"` is what turns "the model
started emitting a differently-named field" into a failure instead of a
silently dropped workout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.training.activities import ActivityType, LoadMode, SetType
from app.domain.training.input_contract import (
    SCHEMA_VERSION,
    StructuredActivityInput,
    StructuredSetInput,
    StructuredWorkoutInput,
)
from app.domain.training.provenance import ExerciseGroupType

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "structured_workout_input.json"
FIXTURE: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_the_fixture_parses() -> None:
    parsed = StructuredWorkoutInput.model_validate(FIXTURE)

    assert parsed.schema_version == SCHEMA_VERSION
    assert len(parsed.activities) == 4


def test_the_fixture_survives_a_round_trip_unchanged() -> None:
    """Byte-identical after parse and dump: a field the model quietly renames
    or reorders is a field Sprint 3 would emit and this would not read."""
    parsed = StructuredWorkoutInput.model_validate(FIXTURE)

    assert parsed.model_dump(mode="json") == FIXTURE


def test_an_unknown_field_is_an_error_rather_than_an_ignore() -> None:
    """A producer that starts emitting `weight_kg` alongside `load` must fail
    loudly. Ignoring it loses the workout and nothing reports the loss."""
    broken = json.loads(json.dumps(FIXTURE))
    broken["activities"][0]["sets"][0]["weight_kg"] = 80

    with pytest.raises(ValidationError, match="weight_kg"):
        StructuredWorkoutInput.model_validate(broken)


def test_a_schema_version_this_code_does_not_know_is_refused() -> None:
    """The version exists so a future contract change is a rejection here
    instead of a misread field somewhere downstream."""
    broken = json.loads(json.dumps(FIXTURE))
    broken["schema_version"] = "workout-input.v2"

    with pytest.raises(ValidationError):
        StructuredWorkoutInput.model_validate(broken)


def test_an_activity_needs_a_name_to_be_worth_anything() -> None:
    with pytest.raises(ValidationError):
        StructuredActivityInput.model_validate({"raw_name": ""})


def test_the_contract_is_immutable_once_parsed() -> None:
    """The builder passes these around and must not be able to edit the input
    it was given: a mutated contract makes the deferral report describe
    something other than what arrived."""
    parsed = StructuredWorkoutInput.model_validate(FIXTURE)

    with pytest.raises(ValidationError):
        parsed.activities[0].sets[0].repetitions = 99


def test_the_fixture_covers_every_shape_a_producer_must_emit() -> None:
    """One file Sprint 3 can read end to end: inheritance, bodyweight, a
    distance activity, a superset, and text nobody could structure."""
    parsed = StructuredWorkoutInput.model_validate(FIXTURE)
    by_name = {activity.raw_name: activity for activity in parsed.activities}

    inheriting = by_name["supino reto"]
    assert inheriting.sets[1].load == "80kg"
    assert inheriting.sets[2].load is None, "the later sets inherit rather than restate"
    assert inheriting.sets[0].set_type is SetType.WARMUP

    bodyweight = by_name["barra fixa"]
    assert bodyweight.sets[0].load is None
    assert bodyweight.effort == "RPE 9", "effort at the activity level (§14.3)"

    distance = by_name["corrida"]
    assert distance.activity_type is ActivityType.DISTANCE_ACTIVITY
    assert (distance.sets[0].distance, distance.sets[0].duration) == ("5km", "25:00")

    assert parsed.groups[0].group_type is ExerciseGroupType.SUPERSET
    assert parsed.groups[0].rounds == 3
    assert {activity.group_ref for activity in parsed.activities if activity.group_ref} == {"A"}

    assert parsed.unparsed == ("depois alonguei um pouco",)


def test_nothing_in_the_contract_asks_a_producer_to_do_arithmetic() -> None:
    """`load` is the raw string the user said. Asking a model for kilograms is
    asking it to be wrong in a way nothing downstream can detect."""
    field = StructuredSetInput.model_fields["load"]

    assert field.annotation == (str | None)


def test_a_load_mode_may_be_stated_but_never_has_to_be() -> None:
    """Q49: the catalog decides, unless the user was explicit."""
    stated = StructuredSetInput.model_validate({"load": "20kg", "load_mode": "per_implement"})

    assert stated.load_mode is LoadMode.PER_IMPLEMENT
    assert StructuredSetInput().load_mode is None
