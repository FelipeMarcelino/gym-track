"""The `session-expiration-worker` process (§18, Q31).

Follows the outbox publisher's shape: one runtime, the shared shutdown event
from `entrypoints.runtime` — never a signal handler of its own, which is the
WS-13 lesson from Sprint 1 — and a loop that ends when asked.
"""

from __future__ import annotations

from app.application.services.training_sessions import TrainingSessionManager
from app.config import ServiceName
from app.entrypoints.runtime import run, shutdown_event, worker_runtime
from app.infrastructure.redis.session_hints import RedisSessionHintStore
from app.workers.session_expiration_worker import SessionExpirationWorker


async def main() -> None:
    from redis.asyncio import Redis

    async with worker_runtime(ServiceName.SESSION_EXPIRATION_WORKER) as runtime:
        redis = Redis.from_url(runtime.settings.redis.url())
        worker = SessionExpirationWorker(
            session_factory=runtime.session_factory,
            manager=TrainingSessionManager(
                timeout=runtime.settings.workflow.training_session_timeout
            ),
            hints=RedisSessionHintStore(
                redis, timeout=runtime.settings.workflow.training_session_timeout
            ),
        )
        try:
            await worker.run_forever(shutdown_event())
        finally:
            await redis.aclose()


if __name__ == "__main__":  # pragma: no cover
    run(main)
