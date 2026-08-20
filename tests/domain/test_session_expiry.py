"""WS-5: when a training session has gone quiet (§18).

The policy is arithmetic and lives alone, away from the database, because the
interesting cases are the boundaries and they are cheap to pin here: one second
before the timeout, exactly on it, one second after.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.training.sessions import SessionCloseReason, expiry_deadline, is_expired

TIMEOUT = timedelta(hours=3)
LAST_ACTIVITY = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (timedelta(0), False),
        (TIMEOUT - timedelta(seconds=1), False),
        (TIMEOUT, False),
        (TIMEOUT + timedelta(seconds=1), True),
        (timedelta(days=2), True),
    ],
    ids=["just now", "one second before", "exactly on it", "one second after", "much later"],
)
def test_expiry_is_decided_at_the_boundary(elapsed: timedelta, expected: bool) -> None:
    """Exactly on the timeout is *not* expired: a session that went quiet three
    hours ago to the second is still the session the user is in, and closing it
    a moment early splits one workout into two."""
    assert (
        is_expired(last_activity_at=LAST_ACTIVITY, now=LAST_ACTIVITY + elapsed, timeout=TIMEOUT)
        is expected
    )


def test_the_deadline_is_measured_from_the_last_activity() -> None:
    """Not from the start: a session somebody is still using does not expire
    because it has been open a long time (§18)."""
    assert expiry_deadline(last_activity_at=LAST_ACTIVITY, timeout=TIMEOUT) == (
        LAST_ACTIVITY + TIMEOUT
    )


def test_a_clock_that_went_backwards_does_not_expire_anything() -> None:
    """Container clocks resync. A `now` behind the last activity is a broken
    clock, not an expired session, and closing on it would end sessions people
    are in the middle of."""
    assert not is_expired(
        last_activity_at=LAST_ACTIVITY,
        now=LAST_ACTIVITY - timedelta(hours=1),
        timeout=TIMEOUT,
    )


@pytest.mark.parametrize(
    ("last_activity_at", "now"),
    [
        (LAST_ACTIVITY.replace(tzinfo=None), LAST_ACTIVITY),
        (LAST_ACTIVITY, LAST_ACTIVITY.replace(tzinfo=None)),
    ],
    ids=["naive last activity", "naive now"],
)
def test_naive_datetimes_are_refused(last_activity_at: datetime, now: datetime) -> None:
    """A naive datetime here means somebody read a column without its timezone,
    and comparing it against an aware one is the bug that closes every session
    three hours early or never."""
    with pytest.raises(ValueError, match="timezone"):
        is_expired(last_activity_at=last_activity_at, now=now, timeout=TIMEOUT)


def test_the_close_reasons_say_who_acted() -> None:
    """A session that closed itself with nobody attributed is the gap the audit
    trail exists to prevent, so the reason is part of the domain vocabulary."""
    assert {reason.value for reason in SessionCloseReason} == {"lazy", "worker", "explicit"}
