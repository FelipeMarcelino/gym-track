"""The lifecycle of a question (§40.2, Q125).

`ClarificationReason` mirrors a **subset** of `DeferralReason` on purpose. Not
every deferral becomes a suspended workflow: a value nobody could parse in an
otherwise complete activity is answered by asking again in the same breath, not
by pausing and waiting. Reusing the enum would erase that distinction and make
every deferral look resumable.
"""

from __future__ import annotations

from enum import StrEnum


class ClarificationStatus(StrEnum):
    """Open, or closed and why.

    Nothing deletes these rows: "what was the system waiting for when this went
    wrong" is the question they exist to answer, and a resolved question that
    was erased cannot answer it.
    """

    WAITING = "waiting"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


#: Closed, however it closed. Paired with `resolved_at` by a CHECK constraint,
#: so a row cannot claim to be open and resolved at once.
RESOLVED_CLARIFICATION_STATUSES: frozenset[ClarificationStatus] = frozenset(
    {
        ClarificationStatus.ANSWERED,
        ClarificationStatus.CANCELLED,
        ClarificationStatus.EXPIRED,
    }
)


class ClarificationReason(StrEnum):
    """Why the workflow stopped: something missing, or something ambiguous."""

    MISSING_ESSENTIAL_DATA = "missing_essential_data"
    AMBIGUOUS_ENTITY = "ambiguous_entity"
