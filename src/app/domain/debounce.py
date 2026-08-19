"""Debounce window arithmetic, as pure functions (§8, §10, Q11, Q113, DEC-010).

Three rules interact here, and the failure mode when they interact wrongly is
silent: a batch that never flushes, or one that flushes twice and splits a
user's fragments across two workflow executions. So the arithmetic lives here,
free of Redis and RabbitMQ, and is tested on its own before either is involved.

* **Sliding window** -- each new fragment pushes the flush out by 3s, because a
  user typing three lines is one thought.
* **Absolute cap** -- the window can never extend past 10s from its first
  message, or a user who keeps typing is never answered.
* **Generation** -- every fragment bumps a counter, and a scheduled flush
  carries the generation it was scheduled for. A flush whose generation is
  behind is stale: the batch grew after it was scheduled, and a newer flush is
  already on its way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class DebounceWindow:
    """The state a debounce key holds."""

    generation: int
    window_started_at: datetime
    last_message_at: datetime


def next_flush_delay(
    window: DebounceWindow,
    *,
    now: datetime,
    sliding: timedelta,
    absolute: timedelta,
) -> timedelta:
    """How long to wait before attempting the next flush.

    Never past the absolute cap: the sliding window may only push the flush
    later within the budget the cap allows.
    """
    remaining = (window.window_started_at + absolute) - now
    return max(timedelta(0), min(sliding, remaining))


def deadline(window: DebounceWindow, *, absolute: timedelta) -> datetime:
    return window.window_started_at + absolute


def should_flush(
    *,
    scheduled_generation: int,
    window: DebounceWindow,
    now: datetime,
    absolute: timedelta,
) -> bool:
    """Decide whether an arriving flush trigger should act.

    A trigger for the current generation always flushes. A stale one normally
    drops -- a newer trigger exists for the fragment that arrived after it was
    scheduled -- **except** once the absolute cap has passed, where dropping it
    could leave the batch waiting on a trigger that has already been consumed.
    Flushing slightly late is recoverable; never flushing is not.
    """
    if scheduled_generation > window.generation:
        # A trigger from the future means the state was lost and rebuilt (§10
        # makes Redis non-authoritative). Acting on it is safer than ignoring
        # it, since the messages themselves are already durable.
        return True
    if scheduled_generation == window.generation:
        return True
    return now >= deadline(window, absolute=absolute)


def is_stale(*, scheduled_generation: int, window: DebounceWindow) -> bool:
    return scheduled_generation < window.generation


def key_for(user_id: str, conversation_id: str) -> str:
    """`debounce:v1:user:{id}:conversation:{id}` (§10).

    The version segment is what lets the state's shape change without a
    migration: a new version writes new keys and the old ones expire.
    """
    return f"debounce:v1:user:{user_id}:conversation:{conversation_id}"
