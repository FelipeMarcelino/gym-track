"""WS-11: a workout from the webhook to the reply (§38).

Sprint 1 proved a message could travel the pipeline. This proves a *workout*
does: the sets are in the database, their provenance is right, they are
reachable from the message that caused them, and the user is told what was
recorded — all read from outside the components that wrote them, because a
component reporting on its own work can be wrong in exactly the way this test
exists to catch.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.graphs.main.handlers import ACKNOWLEDGEMENT
from app.infrastructure.postgres.models import MessageBatch
from app.infrastructure.rabbitmq.partitioning import queue_for_user
from app.infrastructure.rabbitmq.topology import DEBOUNCE_FLUSH_QUEUE
from tests.e2e.conftest import Skeleton

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


async def _run(skeleton: Skeleton, text: str) -> None:
    """The whole pipeline, one process at a time, over real infrastructure."""
    assert (await skeleton.send_webhook(f"wamid.{uuid4()}", text)).status_code == 202

    await skeleton.publish_outbox()
    await skeleton.drain("message.received", skeleton.aggregator.on_message_received)

    await asyncio.sleep(skeleton.settings.workflow.debounce_window.total_seconds() + 1)
    await skeleton.drain(DEBOUNCE_FLUSH_QUEUE, skeleton.aggregator.on_flush)

    await skeleton.publish_outbox()
    async with skeleton.session_factory() as session:
        user_id = await session.scalar(sa.select(MessageBatch.user_id))
    assert user_id is not None, "the pipeline was expected to have produced a batch"
    await skeleton.drain(queue_for_user(user_id, skeleton.partitions), skeleton.worker.handle)

    await skeleton.publish_outbox()
    await skeleton.drain("outbound.dispatch", skeleton.dispatcher.dispatch)


async def test_a_logged_workout_reaches_the_database_and_comes_back(
    skeleton: Skeleton,
) -> None:
    """The one that matters: three sets, in one session, reachable from the
    message, confirmed to the user by name."""
    await _run(skeleton, "#log supino 80kg 10 9 8")

    rows = await skeleton.rows(
        """
        SELECT e.canonical_name, es.set_index, es.repetitions, es.load_kg, es.load_provenance
        FROM exercise_sets es
        JOIN session_exercises se ON se.id = es.session_exercise_id
        JOIN exercises e ON e.id = se.exercise_id
        JOIN entity_sources src
          ON src.entity_id = es.id AND src.entity_type = 'exercise_set'
        JOIN messages m ON m.id = src.message_id
        WHERE m.text = :text
        ORDER BY es.set_index
        """,
        text="#log supino 80kg 10 9 8",
    )

    assert len(rows) == 3, "the sets must be reachable from the message that caused them"
    assert [row[0] for row in rows] == ["Supino reto"] * 3
    assert [row[2] for row in rows] == [10, 9, 8]

    # §14.4 through the whole pipeline: the user stated one load and we carried
    # it, and the database records which is which.
    assert [row[4] for row in rows] == ["explicit", "inherited", "inherited"]

    assert len(skeleton.whatsapp.texts) == 1
    assert "Supino reto" in skeleton.whatsapp.texts[0]


async def test_one_workout_opens_exactly_one_session(skeleton: Skeleton) -> None:
    await _run(skeleton, "#log supino 80kg 10 9 8")

    sessions = await skeleton.rows("SELECT id, status FROM training_sessions")

    assert len(sessions) == 1
    assert sessions[0][1] == "active"


async def test_the_interaction_stays_traceable_with_a_real_handler(
    skeleton: Skeleton,
) -> None:
    """Q131 held for the stub in Sprint 1. It has to still hold now that the
    handler does actual work, or an operator loses the thread exactly where it
    became worth following."""
    await _run(skeleton, "#log supino 80kg 10 9 8")

    traces = await skeleton.rows(
        """
        SELECT mb.trace_id, we.trace_id, om.trace_id
        FROM message_batches mb
        JOIN workflow_executions we ON we.message_batch_id = mb.id
        JOIN outbound_messages om ON om.workflow_execution_id = we.id
        """
    )

    assert len(traces) == 1
    assert len(set(traces[0])) == 1, "one interaction, one trace"


async def test_an_incomplete_workout_asks_instead_of_writing(skeleton: Skeleton) -> None:
    """ "#log supino 80kg" says a weight and no repetitions. Q46: the reply asks
    for the missing datum, and nothing is written in the meantime."""
    await _run(skeleton, "#log supino 80kg")

    sets = await skeleton.rows("SELECT id FROM exercise_sets")
    blocks = await skeleton.rows("SELECT id FROM session_exercises")

    assert sets == []
    assert blocks == []
    assert len(skeleton.whatsapp.texts) == 1
    assert "repetições" in skeleton.whatsapp.texts[0]


async def test_an_ordinary_message_still_gets_the_sprint_one_reply(
    skeleton: Skeleton,
) -> None:
    """Routing must not have swallowed everything that is not a workout."""
    await _run(skeleton, "bom dia")

    assert await skeleton.rows("SELECT id FROM training_sessions") == []
    assert len(skeleton.whatsapp.texts) == 1
    assert skeleton.whatsapp.texts[0] == ACKNOWLEDGEMENT
