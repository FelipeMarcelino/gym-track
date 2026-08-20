"""Expiry hints in Redis — a suggestion, never an authority (§18).

§18 says PostgreSQL's `last_activity_at` decides whether a session has ended,
and Redis "may provide expiration hints". The distinction is the whole reason
this module is small: a hint tells the sweep which users to look at *first*,
and the sweep follows it with an unfiltered pass regardless. It is a priority,
not a filter — treating it as a filter would mean a user whose hint expired
keeps an open session for as long as other hints remain. Losing the whole
keyspace costs a slower sweep, never a session that stays open.

Every key carries a TTL slightly longer than the timeout it describes, so a
hint cannot outlive its own relevance by much — and even when one does, the
manager re-checks the authority before acting.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final
from uuid import UUID

from redis.asyncio import Redis

KEY_PREFIX: Final = "session:v1:expiry-hint:user:"

#: Slack over the timeout: a hint is allowed to survive slightly past the
#: deadline it describes, because the sweep runs on its own schedule.
TTL_SLACK: Final = timedelta(minutes=5)


class RedisSessionHintStore:
    def __init__(self, redis: Redis, *, timeout: timedelta) -> None:
        self._redis = redis
        self._timeout = timeout

    async def note_activity(self, user_id: UUID) -> None:
        """Remember that this user was active, so the sweep can find them later."""
        await self._redis.set(
            f"{KEY_PREFIX}{user_id}",
            "1",
            ex=int((self._timeout + TTL_SLACK).total_seconds()),
        )

    async def expiry_candidates(self, *, limit: int = 100) -> list[UUID]:
        """Users worth checking. Wrong answers here are harmless by design.

        A user missing from the list is checked by the unfiltered pass that
        follows; a user wrongly present is refused by `last_activity_at`.
        Neither costs correctness, which is what makes it safe to keep in a store §10 already
        declares non-authoritative.
        """
        found: list[UUID] = []
        async for key in self._redis.scan_iter(match=f"{KEY_PREFIX}*", count=limit):
            raw = key.decode() if isinstance(key, bytes) else str(key)
            try:
                found.append(UUID(raw.removeprefix(KEY_PREFIX)))
            except ValueError:  # pragma: no cover - a key written by something else
                continue
            if len(found) >= limit:
                break
        return found
