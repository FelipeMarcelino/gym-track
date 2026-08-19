"""Debounce state in Redis (§10, Q11, DEC-010).

§10 declares Redis non-authoritative, and this module is written as if it means
it: the state here is a *hint* about which messages are still waiting, and the
messages themselves are already durable in PostgreSQL. Losing the whole
keyspace costs a late flush, never a lost message.

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
        message_id: UUID,
        now: datetime | None = None,
    ) -> RegisteredMessage:
        """Add a fragment to the window, bumping the generation.

        Done in one pipeline: the generation bump and the membership append
        must not be separable, or a flush could read a generation that does not
        describe the messages it is about to take.
        """
        moment = now or datetime.now(UTC)
        key = key_for(str(user_id), str(conversation_id))
        ttl = self._absolute + TTL_SLACK

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hsetnx(key, "window_started_at", moment.isoformat())
            pipe.hincrby(key, "generation", 1)
            pipe.hset(key, "last_message_at", moment.isoformat())
            pipe.rpush(self._members_key(key), str(message_id))
            pipe.expire(key, ttl)
            pipe.expire(self._members_key(key), ttl)
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

    async def take(self, *, user_id: UUID, conversation_id: UUID) -> list[UUID]:
        """Claim the window's members and clear it, atomically.

        Clearing on read is what stops two flushes from producing two batches
        out of one window.
        """
        key = key_for(str(user_id), str(conversation_id))
        members_key = self._members_key(key)

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.lrange(members_key, 0, -1)
            pipe.delete(members_key)
            pipe.delete(key)
            results = await pipe.execute()

        return [UUID(_text(raw)) for raw in results[0]]

    async def ttl_of(self, *, user_id: UUID, conversation_id: UUID) -> int:
        """Remaining TTL in seconds; -1 means no expiry, -2 means no key."""
        return int(await self._redis.ttl(key_for(str(user_id), str(conversation_id))))

    async def _window_start(self, key: str, fallback: datetime) -> datetime:
        stored = await self._redis.hget(key, "window_started_at")
        if stored is None:  # pragma: no cover - the key was just written
            return fallback
        return datetime.fromisoformat(_text(stored))

    @staticmethod
    def _members_key(key: str) -> str:
        return f"{key}:messages"
