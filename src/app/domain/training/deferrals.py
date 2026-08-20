"""What could not be recorded, and why (Q46, Q56, Q57).

A deferral is a domain fact, not a plumbing detail: it says an activity the
user described did not become a row and names what would make it one. Q57 makes
it the normal outcome rather than an error — an activity nobody could
understand must not cost the user the ones we did — so this travels beside the
command rather than instead of it.

It lives in the domain because the pt-BR confirmations (D8) are pure functions
of it, and a message module that had to import the application layer to read a
reason would have the dependency pointing the wrong way.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.exercises.resolution import ResolutionCandidate
from app.domain.training.activities import ActivityField


class DeferralReason(StrEnum):
    UNRESOLVED_EXERCISE = "unresolved_exercise"
    AMBIGUOUS_EXERCISE = "ambiguous_exercise"
    MISSING_ESSENTIAL_DATA = "missing_essential_data"
    INVALID_VALUE = "invalid_value"


@dataclass(frozen=True, slots=True)
class DeferredItem:
    raw_name: str
    reason: DeferralReason
    #: What to ask for, when the answer is a missing measurement (Q46). Carried
    #: rather than re-derived, so whoever writes the reply is not re-running
    #: the validator to find out what it already knew.
    missing_field: ActivityField | None = None
    #: What to offer, when the answer is a choice between exercises (Q56).
    candidates: tuple[ResolutionCandidate, ...] = ()
