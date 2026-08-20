"""WS-11: what survives things going wrong (§38, DEC-005, §28).

Sprint 1 proved the pipeline was atomic for an acknowledgement. These prove it
for a workout, which is the first thing in this system whose loss the user
would actually notice.

Each case injects a specific failure and asserts on the database from outside.
The claim under test is not that the code is careful — it is that the failure
window does not exist, because the write is one transaction.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.infrastructure.postgres.models import MessageBatch
from app.infrastructure.rabbitmq.partitioning import queue_for_user
from app.infrastructure.rabbitmq.topology import DEBOUNCE_FLUSH_QUEUE
from tests.e2e.conftest import Skeleton

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

WORKOUT = "#log supino 80kg 10 9 8"


async def _to_the_worker(skeleton: Skeleton, text: str = WORKOUT) -> str:
    """Everything up to the workflow queue, leaving the batch waiting there."""
    assert (await skeleton.send_webhook(f"wamid.{uuid4()}", text)).status_code == 202

    await skeleton.publish_outbox()
    await skeleton.drain("message.received", skeleton.aggregator.on_message_received)
    await asyncio.sleep(skeleton.settings.workflow.debounce_window.total_seconds() + 1)
    await skeleton.drain(DEBOUNCE_FLUSH_QUEUE, skeleton.aggregator.on_flush)
    await skeleton.publish_outbox()

    async with skeleton.session_factory() as session:
        user_id = await session.scalar(sa.select(MessageBatch.user_id))
    assert user_id is not None
    return queue_for_user(user_id, skeleton.partitions)


async def _count(skeleton: Skeleton, table: str) -> int:
    rows = await skeleton.rows(f"SELECT count(*) FROM {table}")
    return int(rows[0][0])


async def test_a_redelivered_batch_does_not_double_the_workout(
    skeleton: Skeleton,
) -> None:
    """At-least-once delivery is the contract (DEC-005). The user's three sets
    have to stay three sets however many times the broker hands the batch over."""
    queue = await _to_the_worker(skeleton)

    delivered: list[dict[str, Any]] = []

    async def recording(body: dict[str, Any]) -> Any:
        delivered.append(body)
        return await skeleton.worker.handle(body)

    assert await skeleton.drain(queue, recording) == 1

    # The same envelope, byte for byte, exactly as a broker redelivery arrives.
    # Reconstructing one by hand would test a shape production never publishes.
    await skeleton.worker.handle(delivered[0])

    assert await _count(skeleton, "exercise_sets") == 3
    assert await _count(skeleton, "session_exercises") == 1
    assert await _count(skeleton, "training_sessions") == 1

    logged = await skeleton.rows(
        "SELECT count(*) FROM domain_events WHERE event_type = 'workout.logged'"
    )
    assert logged[0][0] == 1, "one workout, one announcement"


async def test_a_crash_between_the_sets_and_the_outbox_leaves_nothing(
    skeleton: Skeleton, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§38's hardest case, and the reason the write is one transaction.

    The failure is injected *after* the sets have been added and before the
    outbox row exists — the exact window in which a two-step implementation
    would leave a workout nobody was told about. There is nothing to assert
    about recovery, because there is nothing to recover: the transaction never
    commits.
    """
    queue = await _to_the_worker(skeleton)

    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the outbox write failed")

    monkeypatch.setattr("app.application.services.workout_logging.record_domain_event", explode)

    with pytest.raises(RuntimeError, match="outbox"):
        await skeleton.drain(queue, skeleton.worker.handle)

    assert await _count(skeleton, "exercise_sets") == 0
    assert await _count(skeleton, "session_exercises") == 0
    assert await _count(skeleton, "training_sessions") == 0
    assert await _count(skeleton, "audit_events") == 0
    assert await _count(skeleton, "processed_operations") == 0
    assert await _count(skeleton, "workflow_executions") == 0


async def test_the_batch_can_be_processed_after_the_crash_is_fixed(
    skeleton: Skeleton, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same claim: because nothing committed, the
    redelivery that follows a fixed deployment writes the workout normally.
    A failure that had left the idempotency claim behind would have made the
    user's training unrecoverable."""
    queue = await _to_the_worker(skeleton)

    delivered: list[dict[str, Any]] = []

    async def recording(body: dict[str, Any]) -> Any:
        delivered.append(body)
        return await skeleton.worker.handle(body)

    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the outbox write failed")

    monkeypatch.setattr("app.application.services.workout_logging.record_domain_event", explode)
    with pytest.raises(RuntimeError):
        await skeleton.drain(queue, recording)

    monkeypatch.undo()
    await skeleton.worker.handle(delivered[0])

    assert await _count(skeleton, "exercise_sets") == 3


async def test_publishing_the_same_event_twice_writes_nothing_twice(
    skeleton: Skeleton,
) -> None:
    """The outbox publisher is at-least-once too. Draining it repeatedly must
    not produce a second reply to the user."""
    queue = await _to_the_worker(skeleton)
    await skeleton.drain(queue, skeleton.worker.handle)

    await skeleton.publish_outbox()
    await skeleton.publish_outbox()
    await skeleton.drain("outbound.dispatch", skeleton.dispatcher.dispatch)
    await skeleton.drain("outbound.dispatch", skeleton.dispatcher.dispatch)

    assert await _count(skeleton, "exercise_sets") == 3
    assert len(skeleton.whatsapp.texts) == 1, "one workout, one confirmation"
