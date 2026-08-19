"""Debounce state in Redis (§10, Q11, DEC-010).

§10 declares Redis non-authoritative, and this module is written as if it means
it. What lives here is **only timing**: when the window opened, when it was
last touched, and which generation is current. Batch *membership* is never
stored here -- it is derived from `messages` at flush time -- so losing the
keyspace costs a late batch and never a lost message.

Every write sets a TTL. A debounce key that outlives its window is a leak that
only shows up as memory pressure weeks later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis

from app.domain.debounce import DebounceWindow, key_for


def _text(value: bytes | str) -> str:
    """redis-py returns bytes unless the client decodes responses; accept both."""
    return value.decode() if isinstance(value, bytes) else value


#: Slack on top of the absolute window, so a key survives a flush that is
#: running slightly late but never lingers.
TTL_SLACK = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class RegisteredMessage:
    window: DebounceWindow
    #: True when this message opened the window, which is when a first flush
    #: has to be scheduled.
    opened_window: bool


class RedisDebounceStore:
    def __init__(self, redis: Redis, *, absolute_window: timedelta) -> None:
        self._redis = redis
        self._absolute = absolute_window

    async def register(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        now: datetime | None = None,
    ) -> RegisteredMessage:
        """Note that a fragment arrived: open the window and bump the generation."""
        moment = now or datetime.now(UTC)
        key = key_for(str(user_id), str(conversation_id))
        ttl = self._absolute + TTL_SLACK

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hsetnx(key, "window_started_at", moment.isoformat())
            pipe.hincrby(key, "generation", 1)
            pipe.hset(key, "last_message_at", moment.isoformat())
            pipe.expire(key, ttl)
            results = await pipe.execute()

        opened_window = bool(results[0])
        generation = int(results[1])
        started_at = moment if opened_window else await self._window_start(key, moment)

        return RegisteredMessage(
            window=DebounceWindow(
                generation=generation,
                window_started_at=started_at,
                last_message_at=moment,
            ),
            opened_window=opened_window,
        )

    async def read(self, *, user_id: UUID, conversation_id: UUID) -> DebounceWindow | None:
        key = key_for(str(user_id), str(conversation_id))
        stored = await self._redis.hgetall(key)
        if not stored:
            return None
        return DebounceWindow(
            generation=int(stored[b"generation"]),
            window_started_at=datetime.fromisoformat(_text(stored[b"window_started_at"])),
            last_message_at=datetime.fromisoformat(_text(stored[b"last_message_at"])),
        )

    async def close(self, *, user_id: UUID, conversation_id: UUID) -> None:
        """Drop the window, once its batch is durably committed.

        Deliberately called *after* the database transaction: if the window
        were cleared first, a failure mid-commit would leave a retried trigger
        with no window to act on, and the fragments would wait for a future
        message that may never come.
        """
        await self._redis.delete(key_for(str(user_id), str(conversation_id)))

    async def ttl_of(self, *, user_id: UUID, conversation_id: UUID) -> int:
        """Remaining TTL in seconds; -1 means no expiry, -2 means no key."""
        return int(await self._redis.ttl(key_for(str(user_id), str(conversation_id))))

    async def _window_start(self, key: str, fallback: datetime) -> datetime:
        stored = await self._redis.hget(key, "window_started_at")
        if stored is None:  # pragma: no cover - the key was just written
            return fallback
        return datetime.fromisoformat(_text(stored))
