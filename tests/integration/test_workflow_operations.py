"""WS-2: what the database refuses about a workflow's operational state.

§11.2 and Q118/Q125/Q128 ask for task and clarification state that lives
*outside* the LangGraph checkpoint, so "what is this workflow doing" and "what
is it waiting for" are SQL questions. Everything asserted here is asserted
against a real PostgreSQL: an invariant only Python knows about is a comment,
and these particular invariants are the ones a resumed workflow depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.clarification.status import ClarificationReason, ClarificationStatus
from app.domain.results import ResultVisibility, TaskType
from app.domain.workflow.tasks import TaskStatus
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    COMMITTED_WORKFLOW_OUTCOMES,
    FINISHED_WORKFLOW_STATUSES,
    Conversation,
    ExecutionTask,
    MessageBatch,
    PendingClarification,
    User,
    WorkflowExecution,
    WorkflowExecutionStatus,
)

pytestmark = [pytest.mark.integration]


class _Workflow:
    def __init__(self, *, user_id: UUID, conversation_id: UUID, execution_id: UUID) -> None:
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.execution_id = execution_id


async def _new_execution(
    session: AsyncSession, *, user_id: UUID, conversation_id: UUID
) -> WorkflowExecution:
    batch = MessageBatch(user_id=user_id, conversation_id=conversation_id)
    session.add(batch)
    await session.flush()
    execution = WorkflowExecution(
        message_batch_id=batch.id,
        user_id=user_id,
        conversation_id=conversation_id,
        status=WorkflowExecutionStatus.RUNNING,
        graph_version="main.v1",
    )
    session.add(execution)
    await session.flush()
    return execution


@pytest.fixture
async def workflow(session_factory: async_sessionmaker[AsyncSession]) -> _Workflow:
    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        execution = await _new_execution(session, user_id=user.id, conversation_id=conversation.id)
        return _Workflow(
            user_id=user.id, conversation_id=conversation.id, execution_id=execution.id
        )


def _task(execution_id: UUID, key: str = "log_workout", **overrides: object) -> ExecutionTask:
    values: dict[str, object] = {
        "workflow_execution_id": execution_id,
        "task_key": key,
        "task_type": TaskType.LOG_WORKOUT,
        "status": TaskStatus.PENDING,
        "result_visibility": ResultVisibility.USER_VISIBLE,
    }
    values.update(overrides)
    return ExecutionTask(**values)


def _clarification(
    workflow: _Workflow, *, task_key: str = "log_workout", **overrides: object
) -> PendingClarification:
    values: dict[str, object] = {
        "clarification_id": f"clarification-{uuid4()}",
        "workflow_execution_id": workflow.execution_id,
        "user_id": workflow.user_id,
        "conversation_id": workflow.conversation_id,
        "task_key": task_key,
        "reason": ClarificationReason.MISSING_ESSENTIAL_DATA,
        "status": ClarificationStatus.WAITING,
        "spec": {"schema_version": "clarification-spec.v1"},
        "expires_at": datetime.now(UTC) + timedelta(hours=6),
        "checkpoint_ns": f"{workflow.execution_id}:{uuid4()}",
        "checkpoint_id": str(uuid4()),
    }
    values.update(overrides)
    return PendingClarification(**values)


async def test_one_open_question_per_conversation(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """Two open questions make "the answer" ambiguous with no deterministic way
    to disambiguate, so the database refuses the second rather than the
    resolver guessing later (D11)."""
    async with unit_of_work(session_factory) as session:
        session.add(_task(workflow.execution_id))
        await session.flush()
        session.add(_clarification(workflow))

    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(_clarification(workflow))


async def test_a_second_question_is_allowed_once_the_first_is_answered(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """The index is partial for a reason: the constraint is on *open*
    questions, not on how many a conversation may ever have."""
    async with unit_of_work(session_factory) as session:
        session.add(_task(workflow.execution_id))
        await session.flush()
        session.add(_clarification(workflow))

    async with unit_of_work(session_factory) as session:
        row = await session.scalar(sa.select(PendingClarification))
        assert row is not None
        row.status = ClarificationStatus.ANSWERED
        row.resolved_at = datetime.now(UTC)

    async with unit_of_work(session_factory) as session:
        session.add(_clarification(workflow))

    async with unit_of_work(session_factory) as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(PendingClarification)) == 2
        )


async def test_an_open_question_must_not_claim_to_be_resolved(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """`resolved_at` and WAITING are two statements of the same fact, and a row
    that disagrees with itself is what makes an expiry sweep unwritable."""
    async with unit_of_work(session_factory) as session:
        session.add(_task(workflow.execution_id))

    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(_clarification(workflow, resolved_at=datetime.now(UTC)))


async def test_a_resolved_question_must_say_when(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(_task(workflow.execution_id))
            await session.flush()
            session.add(_clarification(workflow, status=ClarificationStatus.EXPIRED))


async def test_a_clarification_belongs_to_a_task_of_its_own_execution(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """The composite foreign key. A clarification pointing at a `task_key` that
    exists under a *different* execution is how a resume ends up completing
    somebody else's task."""
    async with unit_of_work(session_factory) as session:
        other = await _new_execution(
            session, user_id=workflow.user_id, conversation_id=workflow.conversation_id
        )
        session.add(_task(other.id, "elsewhere"))

    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(_clarification(workflow, task_key="elsewhere"))


async def test_a_finished_task_must_say_when(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """A terminal task with no `finished_at` makes "how long did this take"
    unanswerable for every task that follows it."""
    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(_task(workflow.execution_id, status=TaskStatus.COMPLETED))


async def test_a_task_still_running_must_not_claim_to_have_finished(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(
                _task(
                    workflow.execution_id,
                    status=TaskStatus.RUNNING,
                    finished_at=datetime.now(UTC),
                )
            )


async def test_a_waiting_task_has_not_finished(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """WAITING_FOR_USER is not terminal: the task is suspended, and stamping it
    would make "how long do users take to answer" unanswerable."""
    async with unit_of_work(session_factory) as session:
        session.add(_task(workflow.execution_id, status=TaskStatus.WAITING_FOR_USER))

    async with unit_of_work(session_factory) as session:
        task = await session.scalar(sa.select(ExecutionTask))
        assert task is not None and task.finished_at is None


async def test_one_row_per_task_key_within_an_execution(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """What makes `record_plan`'s ON CONFLICT DO NOTHING safe under
    redelivery, and what the composite foreign key references."""
    async with unit_of_work(session_factory) as session:
        session.add(_task(workflow.execution_id))

    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            session.add(_task(workflow.execution_id))


async def test_the_new_workflow_statuses_are_accepted(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    async with unit_of_work(session_factory) as session:
        execution = await session.get(WorkflowExecution, workflow.execution_id)
        assert execution is not None
        execution.status = WorkflowExecutionStatus.WAITING_FOR_USER

    async with unit_of_work(session_factory) as session:
        execution = await session.get(WorkflowExecution, workflow.execution_id)
        assert execution is not None
        execution.status = WorkflowExecutionStatus.PARTIAL_SUCCESS
        execution.finished_at = datetime.now(UTC)


async def test_a_status_outside_the_vocabulary_is_refused(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """The enum is a CHECK constraint, so a value written around the ORM is
    still refused -- which is the only version of the guarantee worth having."""
    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            await session.execute(
                sa.text("UPDATE workflow_executions SET status = 'nonsense' WHERE id = :id"),
                {"id": workflow.execution_id},
            )


async def test_a_terminal_execution_must_say_when_it_finished(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    with pytest.raises(IntegrityError):
        async with unit_of_work(session_factory) as session:
            execution = await session.get(WorkflowExecution, workflow.execution_id)
            assert execution is not None
            execution.status = WorkflowExecutionStatus.SUCCEEDED


async def test_a_paused_execution_has_not_finished(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """WAITING_FOR_USER is deliberately not terminal (WS-2). An execution
    waiting on a user is paused, not done."""
    async with unit_of_work(session_factory) as session:
        execution = await session.get(WorkflowExecution, workflow.execution_id)
        assert execution is not None
        execution.status = WorkflowExecutionStatus.WAITING_FOR_USER

    async with unit_of_work(session_factory) as session:
        execution = await session.get(WorkflowExecution, workflow.execution_id)
        assert execution is not None and execution.finished_at is None


async def test_an_answer_points_back_at_the_execution_it_resumed(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """WS-9's link: the answer is its own batch and therefore its own
    execution, and without this column the pair is unrecoverable afterwards."""
    async with unit_of_work(session_factory) as session:
        answer = await _new_execution(
            session, user_id=workflow.user_id, conversation_id=workflow.conversation_id
        )
        answer.resumed_execution_id = workflow.execution_id

    async with unit_of_work(session_factory) as session:
        resumed = await session.scalar(
            sa.select(WorkflowExecution).where(
                WorkflowExecution.resumed_execution_id == workflow.execution_id
            )
        )
        assert resumed is not None


async def test_a_committed_delivery_is_not_re_run(
    session_factory: async_sessionmaker[AsyncSession], workflow: _Workflow
) -> None:
    """The set the redelivery gate reads, asserted against the one the CHECK
    constraint enforces.

    They are deliberately different, and the two differences are the point:
    a waiting execution committed durable work without finishing, and a failed
    one finished without committing anything. Reading either set for the
    other's job re-asks a question or drops a retry.
    """
    assert WorkflowExecutionStatus.WAITING_FOR_USER in COMMITTED_WORKFLOW_OUTCOMES
    assert WorkflowExecutionStatus.WAITING_FOR_USER not in FINISHED_WORKFLOW_STATUSES
    assert WorkflowExecutionStatus.FAILED in FINISHED_WORKFLOW_STATUSES
    assert WorkflowExecutionStatus.FAILED not in COMMITTED_WORKFLOW_OUTCOMES

    # And the finished set is exactly what migration 0011 wrote in SQL.
    async with unit_of_work(session_factory) as session:
        definition = await session.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_workflow_executions_finished_when_terminal'"
            )
        )
    assert definition is not None
    for status in FINISHED_WORKFLOW_STATUSES:
        assert status.value in definition
    assert WorkflowExecutionStatus.WAITING_FOR_USER.value not in definition
