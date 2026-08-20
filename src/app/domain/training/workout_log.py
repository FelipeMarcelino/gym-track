"""What a committed workout looks like from the outside (§25).

The result of logging, as the confirmation needs to read it: which session it
landed in, which exercises were written, and how many sets each got. No
database types and no command types — the pt-BR confirmations (D8) are pure
functions of this, and they must not need the application layer to say a
sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LoggedExercise:
    session_exercise_id: UUID
    canonical_name: str
    block_index: int
    set_count: int


@dataclass(frozen=True, slots=True)
class WorkoutLoggedResult:
    training_session_id: UUID
    #: Whether this log opened the session, rather than joining one already open.
    session_opened: bool
    exercises: tuple[LoggedExercise, ...]
    #: True when this operation had already been processed: the rows exist from
    #: the first run and nothing was written now (§28). The user still gets a
    #: confirmation -- their message deserves an answer -- but it must not claim
    #: to have recorded the workout a second time.
    replayed: bool = False

    @property
    def set_count(self) -> int:
        return sum(exercise.set_count for exercise in self.exercises)
