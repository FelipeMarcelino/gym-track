"""The session-expiration worker (§18, Q31).

A background sweep that closes sessions whose inactivity has run out. It is a
*fallback*, not the only mechanism: the next workout input closes a stale
session on its way through, and this exists so a user who stops messaging
entirely still has their workout ended.

It never opens a session, and its database role has no INSERT on
`training_sessions` to make that structural rather than intentional.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.services.training_sessions import TrainingSessionManager
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.redis.session_hints import RedisSessionHintStore

logger = logging.getLogger(__name__)


class SessionExpirationWorker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        manager: TrainingSessionManager,
        hints: RedisSessionHintStore | None = None,
        interval: timedelta = timedelta(minutes=1),
        batch: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._manager = manager
        self._hints = hints
        self._interval = interval
        self._batch = batch

    async def run_once(self) -> list[UUID]:
        """One sweep: the hinted users first, then everybody. Returns what closed.

        Two passes rather than one filtered pass, because a hint is a place to
        look and not the set of sessions that exist. Filtering on it would mean
        a user whose hint expired — or was never written, or fell outside the
        scan — keeps an open session for as long as *other* hints remain, and
        §18 puts that decision in `last_activity_at`, not in Redis.
        """
        closed = await self._sweep(candidates=await self._hinted_users())
        closed.extend(await self._sweep(candidates=None))

        if closed:
            logger.info("expired training sessions closed", extra={"closed": len(closed)})
        return closed

    async def _hinted_users(self) -> list[UUID]:
        """Who to check first, or nobody if Redis cannot say.

        A Redis outage costs a slower sweep and nothing else — the pass below
        it is unfiltered, so losing the whole keyspace never leaves a session
        open (§10 declares the store non-authoritative).
        """
        if self._hints is None:
            return []
        try:
            return await self._hints.expiry_candidates(limit=self._batch)
        except Exception:
            logger.warning("expiry hints unavailable; sweeping without them", exc_info=True)
            return []

    async def _sweep(self, *, candidates: list[UUID] | None) -> list[UUID]:
        if candidates is not None and not candidates:
            return []
        async with unit_of_work(self._session_factory) as session:
            return await self._manager.close_expired(
                session, limit=self._batch, candidates=candidates
            )

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Sweep until asked to stop, sleeping on the stop event between passes."""
        while not stop.is_set():
            await self.run_once()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval.total_seconds())
