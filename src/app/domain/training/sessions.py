"""When a training session has gone quiet (§18).

Pure arithmetic, deliberately separate from the manager that applies it. §18
puts the authority for this decision in PostgreSQL's `last_activity_at`, and
keeping the rule here means the same policy is used by the lazy path and by the
background sweep — two places that would otherwise drift by a few seconds and
close the same session twice, or never.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum


class SessionCloseReason(StrEnum):
    """Who ended the session.

    Recorded rather than inferred: a session that closed itself with nobody
    attributed is exactly the gap the audit trail exists to prevent.
    """

    #: Closed on the way into the next workout input (Q31's lazy fallback).
    LAZY = "lazy"
    #: Closed by the background sweep.
    WORKER = "worker"
    #: Closed because the user said so. Nothing produces this in Sprint 2.
    EXPLICIT = "explicit"


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must carry a timezone; a naive datetime here means a column was read "
            "without one, and the comparison would be off by the server's offset"
        )


def expiry_deadline(*, last_activity_at: datetime, timeout: timedelta) -> datetime:
    """When this session stops being the one the user is in.

    Measured from the last activity, not from the start: a session somebody is
    still using does not expire because it has been open a long time.
    """
    _require_aware("last_activity_at", last_activity_at)
    return last_activity_at + timeout


def is_expired(*, last_activity_at: datetime, now: datetime, timeout: timedelta) -> bool:
    """Whether the session has been quiet for longer than the timeout.

    Exactly on the deadline is *not* expired. A session that went quiet three
    hours ago to the second is still the user's session, and closing it a
    moment early splits one workout into two.
    """
    _require_aware("last_activity_at", last_activity_at)
    _require_aware("now", now)
    return now > expiry_deadline(last_activity_at=last_activity_at, timeout=timeout)
