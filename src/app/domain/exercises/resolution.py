"""What it means to have resolved an exercise name, and when we refuse to (§16).

The thresholds live here rather than in the service because they are a product
decision, not an implementation detail: they encode how wrong this system is
willing to be. §16 is explicit that resolving to the *wrong* exercise is the
worst failure available to it — a wrong row is silently wrong forever, while an
unresolved one asks a question and gets an answer.

So the bands are deliberately conservative, and the result type refuses to
describe an outcome that cannot happen: nothing can be both resolved and
waiting for clarification, and nothing can name an exercise without saying how
it got there.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID


class ResolutionMethod(StrEnum):
    """How the name was resolved. The order is §16's, and it is normative.

    The last three are declared and never returned this sprint. They are here
    so the stored vocabulary is stable across the sprints that add them: a
    column that gains values later is a migration, a column that gains meaning
    later is a bug.
    """

    USER_ALIAS = "user_alias"
    GLOBAL_ALIAS = "global_alias"
    CANONICAL = "canonical"
    FUZZY = "fuzzy"
    VECTOR = "vector"
    LLM = "llm"
    USER_CONFIRMED = "user_confirmed"


#: Stages this sprint does not implement. A test asserts none is ever returned.
UNIMPLEMENTED_METHODS: Final[frozenset[ResolutionMethod]] = frozenset(
    {ResolutionMethod.VECTOR, ResolutionMethod.LLM, ResolutionMethod.USER_CONFIRMED}
)

#: Above this, a fuzzy match is written without asking (D3).
HIGH_CONFIDENCE: Final = 0.90
#: Between the two, the match is offered but never assumed.
MEDIUM_CONFIDENCE: Final = 0.70
#: Two candidates closer together than this have no best answer between them.
AMBIGUITY_MARGIN: Final = 0.02


@dataclass(frozen=True, slots=True)
class ResolutionCandidate:
    exercise_id: UUID
    canonical_name: str
    score: float


@dataclass(frozen=True, slots=True)
class ExerciseResolution:
    raw_name: str
    exercise_id: UUID | None = None
    canonical_name: str | None = None
    method: ResolutionMethod | None = None
    confidence: float = 0.0
    candidates: tuple[ResolutionCandidate, ...] = ()
    #: True only when the caller must ask before anything can be written.
    requires_clarification: bool = False

    def __post_init__(self) -> None:
        if self.exercise_id is not None and self.requires_clarification:
            raise ValueError(
                "a resolution cannot both name an exercise and require clarification; "
                "one of them is what the caller acts on"
            )
        if self.exercise_id is not None and self.canonical_name is None:
            raise ValueError(
                "a resolved exercise must carry its canonical_name; an id alone is a resolution "
                "nothing can show the user or read back in an audit row"
            )
        if self.exercise_id is not None and self.method is None:
            raise ValueError(
                "a resolved exercise must carry the method that resolved it, or the row it "
                "produces cannot be audited later"
            )

    @property
    def resolved(self) -> bool:
        return self.exercise_id is not None
