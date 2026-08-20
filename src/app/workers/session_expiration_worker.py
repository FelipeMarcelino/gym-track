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
        """One sweep. Returns the sessions it closed."""
        candidates = None
        if self._hints is not None:
            # Only ever a narrowing: the hint says where to look, and
            # `last_activity_at` says whether to act (§18).
            candidates = await self._hints.expiry_candidates(limit=self._batch)

        async with unit_of_work(self._session_factory) as session:
            closed = await self._manager.close_expired(
                session, limit=self._batch, candidates=candidates
            )

        if closed:
            logger.info("expired training sessions closed", extra={"closed": len(closed)})
        return closed

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Sweep until asked to stop, sleeping on the stop event between passes."""
        while not stop.is_set():
            await self.run_once()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval.total_seconds())
