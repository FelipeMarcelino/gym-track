"""What a task handler produces (§11.2, §25).

`DomainResult` is the boundary between domain work and response generation. In
this sprint the only handler is a stub, but the *shape* is what Sprint 3's real
handlers plug into, so it is worth getting right now rather than reshaping every
call site later.

`visibility` is the field that matters: a result the user never sees still
records domain events and still advances the workflow, it just produces no
outbound message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ResultVisibility(StrEnum):
    USER_VISIBLE = "user_visible"
    INTERNAL = "internal"


class TaskType(StrEnum):
    """Task types the registry can resolve.

    Only CONVERSATION exists this sprint. The others are declared because §11.3
    fixes their names, and a handler added later should slot into an existing
    name rather than invent a parallel vocabulary.
    """

    CONVERSATION = "conversation"
    LOG_WORKOUT = "log_workout"
    CORRECTION = "correction"
    TRAINING_ANALYSIS = "training_analysis"
    RECOMMENDATION = "recommendation"
    WORKOUT_PROGRAM = "workout_program"


@dataclass(frozen=True, slots=True)
class OutboundText:
    """One message of a response group, in the order it must be delivered."""

    text: str
    sequence: int


@dataclass(frozen=True, slots=True)
class DomainResult:
    task_type: TaskType
    visibility: ResultVisibility
    messages: tuple[OutboundText, ...] = ()
    #: Structured facts the response guard will check against in Sprint 3.
    facts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.visibility is ResultVisibility.INTERNAL and self.messages:
            raise ValueError("an internal result has nothing to say to the user")
        sequences = [message.sequence for message in self.messages]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            raise ValueError("response messages must carry a strictly increasing sequence")
