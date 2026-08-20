"""What a producer hands the domain when somebody logs a workout (§14, D5).

This is the file Sprint 3's extractor will be written against, and WS-10's
strict-syntax adapter already is. Two properties matter more than the field
list:

`extra="forbid"` — an unknown field is an error, not a silent ignore. A model
that starts emitting `weight_kg` alongside `load` would otherwise have half its
output dropped without anything failing, and the loss would surface weeks later
as a user's missing training.

Every field is something a language model can plausibly produce from a
sentence. Nothing here requires an id, a UUID, or a unit conversion: `load` is
the raw string the user said ("80kg", "176lb", "20"), and turning it into
kilograms is the domain's job, not the extractor's. Asking a model for
canonical units is asking it to be wrong occasionally in a way nothing
downstream can detect.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.training.activities import ActivityType, LoadMode, SetType
from app.domain.training.provenance import ExerciseGroupType

SCHEMA_VERSION: Final = "workout-input.v1"

_CONTRACT = ConfigDict(frozen=True, extra="forbid")


class StructuredSetInput(BaseModel):
    model_config = _CONTRACT

    repetitions: int | None = None
    #: As stated: "80kg", "176lb", "20". Parsed by WS-3, not by the producer.
    load: str | None = None
    #: Left None unless the user was explicit. The catalog decides otherwise
    #: (Q49): a dumbbell exercise means per-implement whether or not anybody
    #: said so.
    load_mode: LoadMode | None = None
    set_type: SetType = SetType.WORKING
    distance: str | None = None
    duration: str | None = None
    effort: str | None = None
    notes: str | None = None


class StructuredActivityInput(BaseModel):
    model_config = _CONTRACT

    raw_name: str = Field(min_length=1)
    #: None means "ask the catalog". A producer that guesses this can turn a
    #: run into a strength set and change which fields are required.
    activity_type: ActivityType | None = None
    sets: tuple[StructuredSetInput, ...] = ()
    #: Effort reported for the exercise as a whole (§14.3).
    effort: str | None = None
    group_ref: str | None = None


class StructuredGroupInput(BaseModel):
    model_config = _CONTRACT

    ref: str
    group_type: ExerciseGroupType
    rounds: int | None = None


class StructuredWorkoutInput(BaseModel):
    model_config = _CONTRACT

    schema_version: Literal["workout-input.v1"] = SCHEMA_VERSION
    activities: tuple[StructuredActivityInput, ...]
    groups: tuple[StructuredGroupInput, ...] = ()
    #: Text the producer could not structure, kept so nothing the user said is
    #: silently dropped. The domain never parses it; it exists so a later
    #: sprint can ask about it instead of discovering it was lost.
    unparsed: tuple[str, ...] = ()
