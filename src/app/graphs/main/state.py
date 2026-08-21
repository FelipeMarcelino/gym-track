"""What travels through MainGraph (§11.4, Q27, Q123, Q132).

Two objects with opposite lifetimes, and keeping them apart is the point:

* `MainGraphState` is **checkpointed**. It must hold working state and
  references, never a copy of the database (Q27), and nothing that cannot
  survive serialization.
* `WorkerContext` is **per invocation**. The database session lives here
  because a session inside a checkpointed state object would be serialized,
  and a serialized session is a crash at resume time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from typing_extensions import TypedDict

from app.domain.results import DomainResult, OutboundText
from app.domain.workflow.plan import ExecutionPlan

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import ApplicationSettings

#: Bumped whenever the graph's shape changes. Recorded on every execution row
#: (Q132), because ADR-015 declares the checkpoint non-authoritative and
#: prunable -- traceability that lives only there disappears with it.
GRAPH_VERSION: Final = "main.v1"


def merge_task_results(current: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Fan-in reducer for `task_results` (Q122).

    Written now, before anything runs tasks concurrently, because it is cheap
    to have correct early and expensive to discover missing: two tasks
    resolving into the same key would otherwise keep whichever wrote last.
    """
    return {**(current or {}), **incoming}


class MainGraphState(TypedDict, total=False):
    """§11.4, and nothing beyond it.

    The review question for every field added later is Q27's: *is this a
    reference, or a copy of the database?* No message rows, no user profile, no
    history. Identifiers are strings because they are serialized.
    """

    workflow_execution_id: str
    graph_version: str
    trace_id: str | None
    correlation_id: str | None
    user_id: str
    conversation_id: str
    thread_id: str
    input_batch_id: str
    normalized_input: tuple[str, ...]
    intents: tuple[str, ...]
    execution_plan: ExecutionPlan
    task_results: Annotated[dict[str, Any], merge_task_results]
    pending_interrupt: dict[str, Any] | None
    response_input: tuple[DomainResult, ...]
    outbound_messages: tuple[OutboundText, ...]
    workflow_errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """What the nodes need and the state must not carry.

    **Two execution ids, and they differ exactly on a resume.** An answer to a
    clarification is its own `MessageBatch` and therefore its own execution,
    but the tasks it completes were created by the paused one. Collapsing them
    into a single field puts one of the two writes on the wrong row: either the
    `execution_tasks` transitions go to an execution that has no such tasks --
    which the composite foreign key refuses, loudly -- or the reply is filed
    under the execution that asked the question rather than the delivery that
    produced it, which nothing refuses at all.
    """

    session: AsyncSession
    #: Owns this delivery's outbound rows and its `workflow_executions` status.
    delivery_execution_id: UUID
    #: Owns the `execution_tasks` rows being transitioned. Equal to
    #: `delivery_execution_id` on every path except a resume.
    task_execution_id: UUID
    settings: ApplicationSettings


def thread_id_for(conversation_id: UUID, execution_id: UUID, delivery_id: UUID) -> str:
    """The LangGraph thread this run belongs to.

    Three parts, and each earns its place -- **measured**, not assumed
    (`tests/graph/test_langgraph_semantics.py`):

    * the conversation, because Q123 says a conversation is the persistence
      namespace and a clarification must outlive the training session;
    * the execution, because Q29 lets a paused workflow and an unrelated new
      one coexist in one conversation, and sharing a thread would make the new
      run the checkpoint the answer resumes;
    * the delivery, because a retry after a rolled-back transaction must not
      inherit a checkpoint describing work the database never kept.

    The plan reached for `checkpoint_ns` for the last two. That does not work:
    a run started under a top-level `checkpoint_ns` writes, but `aget_state`
    refuses to read it back -- LangGraph treats the field as a subgraph
    namespace. WS-9 has to read and resume that state, so the composite thread
    id is what ships. ADR-015 -- which owns checkpoint isolation -- records
    the deviation from a literal Q123.
    """
    return f"{conversation_id}:{execution_id}:{delivery_id}"


def thread_for(
    conversation_id: UUID,
    execution_id: UUID,
    delivery_id: UUID,
    *,
    checkpoint_id: str | None = None,
) -> RunnableConfig:
    """The LangGraph config for one invocation.

    `checkpoint_id` is passed only on a resume, and read from
    `pending_clarifications` rather than remembered: forking from the exact
    point of the interrupt is what makes a redelivered answer re-enter the
    handler instead of resuming a half-finished attempt.
    """
    configurable: dict[str, Any] = {
        "thread_id": thread_id_for(conversation_id, execution_id, delivery_id)
    }
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return RunnableConfig(configurable=configurable)


def new_delivery_id() -> UUID:
    """A fresh id per delivery attempt, deliberately not a counter.

    `workflow_executions.attempts` is incremented inside the unit of work, so
    the rollback that strands a checkpoint rolls the counter back too -- and
    the redelivery would compute the same thread and find the advanced
    checkpoint. A value with no fate to share with that transaction is the
    point. Nothing needs to find it again: the only path that does is a
    clarification, and its row records the thread in the same commit.
    """
    return uuid4()


def initial_state(
    *,
    execution_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    message_batch_id: UUID,
    thread_id: str,
    trace_id: str | None,
    correlation_id: str | None,
    texts: Mapping[int, str] | tuple[str, ...],
) -> MainGraphState:
    return MainGraphState(
        workflow_execution_id=str(execution_id),
        graph_version=GRAPH_VERSION,
        trace_id=trace_id,
        correlation_id=correlation_id,
        user_id=str(user_id),
        conversation_id=str(conversation_id),
        thread_id=thread_id,
        input_batch_id=str(message_batch_id),
        normalized_input=tuple(texts) if isinstance(texts, tuple) else tuple(texts.values()),
        task_results={},
        workflow_errors=(),
    )
