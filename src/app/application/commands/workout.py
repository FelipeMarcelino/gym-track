"""What the workout service is asked to commit, and what it could not (Q56, Q57).

Two shapes leave the builder. The command is work that is ready to be written —
resolved, in canonical units, validated. The deferrals are everything else,
each naming the activity it came from and why.

They are separate on purpose. Q57 says an activity nobody could understand must
not cost the user the ones we did understand, so a partial success is the
normal outcome rather than an error: the bench press commits, and the sentence
about "aquele exercício do peito" becomes a question.

`DeferralReason` and `DeferredItem` are re-exported from the domain: they are
facts about the user's input rather than plumbing, and D8's pt-BR messages are
pure functions of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from app.domain.training.activities import ActivityType, LoadMode, SetType
from app.domain.training.deferrals import DeferralReason, DeferredItem
from app.domain.training.effort import NormalizedEffort
from app.domain.training.metrics import DerivedMetric
from app.domain.training.provenance import ExerciseGroupType, Provenance

#: Prefix of every log-workout idempotency key.
OPERATION_PREFIX: Final = "log_workout"


def operation_id_for(message_batch_id: UUID) -> str:
    """The idempotency key a batch implies (DEC-005).

    A function rather than a convention, because two places have to agree on it
    exactly -- the builder that stamps the command and WS-9's handler that
    checks `processed_operations` -- and a redelivery has to reach the same
    string with no state carried between attempts.
    """
    return f"{OPERATION_PREFIX}:{message_batch_id}"


class EmptyCommandError(ValueError):
    """Raised when a command would commit nothing.

    Writing one anyway would open a session, an audit row and a domain event
    describing work that does not exist.
    """


@dataclass(frozen=True, slots=True)
class SetCommand:
    set_index: int
    set_type: SetType
    repetitions: int | None
    repetitions_provenance: Provenance
    load_kg: Decimal | None
    load_mode: LoadMode | None
    load_provenance: Provenance
    #: What the user actually said (§19), kept beside the converted value.
    raw_load_text: str | None
    distance_m: Decimal | None
    distance_provenance: Provenance
    duration_s: Decimal | None
    duration_provenance: Provenance
    effort: NormalizedEffort | None
    metrics: tuple[DerivedMetric, ...] = ()
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class ActivityCommand:
    exercise_id: UUID
    canonical_name: str
    activity_type: ActivityType
    effort: NormalizedEffort | None
    sets: tuple[SetCommand, ...]
    group_ref: str | None = None


@dataclass(frozen=True, slots=True)
class GroupCommand:
    ref: str
    group_type: ExerciseGroupType
    rounds: int | None = None


@dataclass(frozen=True, slots=True)
class LogWorkoutCommand:
    #: `log_workout:{message_batch_id}` — derived rather than generated, so a
    #: redelivery computes the same key without carrying state (DEC-005).
    operation_id: str
    user_id: UUID
    conversation_id: UUID
    message_batch_id: UUID
    source_message_ids: tuple[UUID, ...]
    activities: tuple[ActivityCommand, ...]
    groups: tuple[GroupCommand, ...] = ()

    def __post_init__(self) -> None:
        if self.operation_id != operation_id_for(self.message_batch_id):
            # Documented invariants that nothing checks are invariants until
            # the first caller who did not read the comment. A mismatched key
            # silently disables deduplication, and the second copy of the
            # workout is what tells you.
            raise ValueError(
                f"operation_id must be {operation_id_for(self.message_batch_id)!r} for this "
                f"batch, got {self.operation_id!r}; a key that does not follow from the batch "
                "cannot deduplicate a redelivery"
            )
        if not self.activities:
            raise EmptyCommandError(
                "a log-workout command must carry at least one activity; committing an empty "
                "one would record a session that describes no training"
            )


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    """The result of trying to build a command: what commits, and what asks."""

    command: LogWorkoutCommand | None
    deferred: tuple[DeferredItem, ...] = ()

    @property
    def has_work(self) -> bool:
        return self.command is not None


__all__ = [
    "OPERATION_PREFIX",
    "ActivityCommand",
    "BuildOutcome",
    "DeferralReason",
    "DeferredItem",
    "EmptyCommandError",
    "GroupCommand",
    "LogWorkoutCommand",
    "SetCommand",
    "operation_id_for",
]
