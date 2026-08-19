"""WS-8: the debounce arithmetic, before Redis is anywhere near it (Q113).

This is the subtlest correctness surface in the sprint: sliding window,
absolute cap and generation counter interact, and getting it wrong means either
a batch that never flushes or a user's fragments split across two workflow
executions. Both failures are silent, so the rules are pinned here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.debounce import (
    DebounceWindow,
    deadline,
    is_stale,
    key_for,
    next_flush_delay,
    should_flush,
)

SLIDING = timedelta(seconds=3)
ABSOLUTE = timedelta(seconds=10)
START = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def window(generation: int = 1, *, started_at: datetime = START) -> DebounceWindow:
    return DebounceWindow(
        generation=generation, window_started_at=started_at, last_message_at=started_at
    )


def test_a_fresh_window_waits_the_sliding_interval() -> None:
    assert next_flush_delay(window(), now=START, sliding=SLIDING, absolute=ABSOLUTE) == SLIDING


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (timedelta(0), timedelta(seconds=3)),
        (timedelta(seconds=5), timedelta(seconds=3)),
        (timedelta(seconds=6.9), timedelta(seconds=3)),
        # Past this point the sliding window would cross the cap, so the delay
        # shrinks to whatever is left of the budget.
        (timedelta(seconds=8), timedelta(seconds=2)),
        (timedelta(seconds=9), timedelta(seconds=1)),
        (timedelta(seconds=9.5), timedelta(seconds=0.5)),
        (timedelta(seconds=10), timedelta(0)),
        (timedelta(seconds=30), timedelta(0)),
    ],
)
def test_the_delay_never_reaches_past_the_absolute_cap(
    elapsed: timedelta, expected: timedelta
) -> None:
    """A message at 9s into a 10s cap still respects the absolute window."""
    delay = next_flush_delay(window(), now=START + elapsed, sliding=SLIDING, absolute=ABSOLUTE)

    assert delay == expected


def test_the_deadline_is_measured_from_the_first_message() -> None:
    """Not from the last one: otherwise a user typing steadily is never
    answered, which is the whole reason the cap exists."""
    assert deadline(window(), absolute=ABSOLUTE) == START + ABSOLUTE


def test_a_trigger_for_the_current_generation_flushes() -> None:
    assert should_flush(
        scheduled_generation=3, window=window(3), now=START + SLIDING, absolute=ABSOLUTE
    )


def test_a_stale_trigger_is_dropped_while_the_batch_is_still_growing() -> None:
    """The batch grew after this trigger was scheduled, so a newer trigger is
    already on its way. Flushing here would split the batch."""
    assert not should_flush(
        scheduled_generation=2, window=window(5), now=START + SLIDING, absolute=ABSOLUTE
    )
    assert is_stale(scheduled_generation=2, window=window(5))


def test_a_stale_trigger_still_flushes_once_the_cap_has_passed() -> None:
    """Dropping it there could leave the batch waiting on a trigger that has
    already been consumed. Late is recoverable; never is not."""
    assert should_flush(
        scheduled_generation=2, window=window(5), now=START + ABSOLUTE, absolute=ABSOLUTE
    )


def test_a_trigger_from_the_future_flushes() -> None:
    """§10 makes Redis non-authoritative: if the state was lost and rebuilt, the
    generation restarts below a trigger already in flight. The messages
    themselves are durable, so acting is safer than ignoring."""
    assert should_flush(scheduled_generation=9, window=window(1), now=START, absolute=ABSOLUTE)


@pytest.mark.parametrize("generation", [1, 2, 50])
def test_a_trigger_matching_its_own_generation_is_never_stale(generation: int) -> None:
    assert not is_stale(scheduled_generation=generation, window=window(generation))


def test_the_key_shape_is_the_one_section_10_specifies() -> None:
    assert key_for("u-1", "c-1") == "debounce:v1:user:u-1:conversation:c-1"


def test_the_key_is_versioned_so_the_state_shape_can_change() -> None:
    """A new version writes new keys and the old ones expire on their own —
    Redis state is never migrated."""
    assert key_for("u-1", "c-1").startswith("debounce:v1:")


def test_keys_of_different_conversations_do_not_collide() -> None:
    assert key_for("u-1", "c-1") != key_for("u-1", "c-2")
    assert key_for("u-1", "c-1") != key_for("u-2", "c-1")
