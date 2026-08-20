# Sprint 3 — Task-level Implementation Plan

Companion to `doc/sprints/sprint-03-langgraph-orchestration.md`. The sprint file states *what* ships and *why*; this states *where the code goes*, *what its signatures are*, *which migration carries the schema*, and *which test files fail when it is wrong*.

**Nothing here is normative.** Where this plan and the architecture spec disagree, the spec wins (or the spec gets an ADR). Where this plan and the sprint file disagree, the sprint file wins.

## How to read this

Each workstream lists: **Branch** (per `CLAUDE.md`), **Files** created (`+`) or modified (`~`), **Signatures** (bodies deliberately absent — this is a plan), **Migration** (revision, `down_revision`, what `downgrade()` undoes), **Tests** (file by file, each with its specific assertion), and **Done when** (the mechanical check that closes it).

## Measured, not assumed

Sprint 2 shipped a scorer the plan had named wrongly, and the only reason it was caught is that somebody ran it against the real seed before trusting it. The same rule applies here, and three facts below were **measured before this plan was written** rather than recalled:

| Claim | How it was checked | Result |
| --- | --- | --- |
| `langgraph-checkpoint-postgres` can be told which schema to use | Downloaded the 3.1.2 wheel and searched every module for the string `schema` | **It cannot.** No such parameter exists anywhere in the package |
| Which tables the checkpointer creates | Read `CREATE TABLE IF NOT EXISTS` out of `langgraph/checkpoint/postgres/base.py` | `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations` — all unqualified |
| What the async saver's surface is | Read `langgraph/checkpoint/postgres/aio.py` | `AsyncPostgresSaver(conn, pipe=None, serde=None)`, `AsyncPostgresSaver.from_conn_string(conn_string, *, pipeline=False, serde=None)`, `await saver.setup()` |

Consequences, applied throughout this plan: isolation comes from `search_path` on the checkpointer's own connection (D2), `setup()` is run by the admin identity because it issues DDL (D3), and WS-1's first test asserts *where the four tables actually landed* rather than trusting any of this.

Two more items are named but **not yet measured**; measure them before writing the code they belong to, and record the result in the PR:

- **`interrupt()` semantics under a resumed thread with a modified input.** The plan assumes `Command(resume=value)` re-enters the interrupted node and that nodes before it do not re-run. Verify against the pinned version before WS-8 hardens around it.
- **`psycopg_pool` behaviour under the worker's shutdown path.** The plan assumes an explicit `close()` is enough for §37.4; verify no task is left pending on SIGTERM.

## Decisions applied

| # | Applied as |
| --- | --- |
| D1 | `langgraph>=1.2,<2` and `langgraph-checkpoint-postgres>=3.1,<4` in `[project].dependencies` (WS-1) |
| D2 | `CHECKPOINT_SCHEMA = "langgraph"` in `app/infrastructure/langgraph/checkpointer.py`; the psycopg DSN carries `options=-c%20search_path%3Dlanggraph` |
| D3 | `scripts/checkpointer_setup.py`, invoked by `make migrate` after `alembic upgrade head`, connecting as the admin role |
| D4 | `thread_id=str(conversation_id)` built by one function, `thread_for(conversation_id)`, so no call site can invent its own |
| D5 | `IntentRouterPort` in `app/application/ports/routing.py`; `DeterministicIntentRouter` wraps Sprint 2's `route()` |
| D6 | `DeterministicExecutionPlanner` in `app/application/services/execution_planner.py`; one task per routed intent |
| D7 | `app/graphs/main/interrupts.py` owns every `interrupt()` call; no other module imports it from `langgraph` |
| D8 | `PendingWorkflowResolver` in `app/application/services/pending_workflows.py`; `CANCEL_PHRASES` a frozen tuple in `app/domain/conversation/cancellation.py` |
| D9 | `ResponseNormalizerPort` in `app/application/ports/response.py`; `TemplateResponseNormalizer` is this sprint's implementation; `app/domain/response/guard.py` holds the guard, pure |
| D10 | `WorkflowSettings.clarification_timeout: timedelta = timedelta(hours=6)`, exported as `GYM_TRACK_WORKFLOW__CLARIFICATION_TIMEOUT=PT6H` |
| D11 | `uq_pending_clarifications_open_per_conversation` — partial unique index on `(conversation_id) WHERE status = 'waiting'` |

## Migration and branch order

| WS | Branch | Migration | Depends on |
| --- | --- | --- | --- |
| WS-1 | `feat/ws-1-langgraph-checkpointer` | `0010_langgraph_schema` | `0009` |
| WS-2 | `feat/ws-2-workflow-ops-tables` | `0011_workflow_operations` | `0010` |
| WS-3 | `feat/ws-3-execution-plan` | — | — |
| WS-4 | `feat/ws-4-main-graph` | — | WS-1, WS-3 |
| WS-5 | `feat/ws-5-task-scheduler` | — | WS-2, WS-4 |
| WS-6 | `feat/ws-6-response-guard` | — | WS-4 |
| WS-7 | `feat/ws-7-worker-runs-the-graph` | — | WS-5, WS-6 |
| WS-8 | `feat/ws-8-clarification-interrupts` | — | WS-7 |
| WS-9 | `feat/ws-9-resume-and-pending-workflows` | — | WS-8 |
| WS-10 | `feat/ws-10-dead-letter-replay` | — | — |
| WS-11 | `feat/ws-11-cross-cutting-verification` | — | WS-9 |
| WS-12 | `doc/ws-12-sprint-3-decision-records` | — | WS-1, WS-4, WS-8 |

WS-3, WS-6 and WS-10 touch no schema and no graph wiring; they can be reviewed in parallel with the rest. The table is the safe serial order.

## The lists Sprint 1 left behind

Unchanged from Sprint 2, and still the fastest way to break the suite in a place unrelated to your change. Any workstream that adds a table **must** update, in the same PR:

1. `src/app/infrastructure/postgres/grants.py` — `SERVICE_GRANTS` per role **and** `ALL_TABLES`.
2. `tests/unit/test_persistence_contracts.py` — `EXPECTED_TABLES`.
3. `tests/conftest.py` — the `TRUNCATE` list in `clean_tables`.
4. `src/app/infrastructure/postgres/models.py` — the model.

**New in this sprint:** the four checkpoint tables live in the `langgraph` schema and are **not** SQLAlchemy models, so they belong to none of those four lists. `EXPECTED_TABLES` compares the `public` schema only, and WS-1 adds a test asserting the checkpoint tables are absent from it — otherwise the first `alembic autogenerate` run after WS-1 proposes dropping four tables it does not own.

Adding a value to an enum column is **not** free either: `enum_column` renders a VARCHAR with a named CHECK constraint, so WS-2 must drop `workflowexecutionstatus` by that exact auto-generated name and recreate it — explicitly named this time, per the lesson Sprint 2's WS-6 paid for with four constraints all called `provenance`.

---

# WS-1 — LangGraph runtime and the checkpointer boundary

**Branch:** `feat/ws-1-langgraph-checkpointer`

## Files

```text
+ src/app/infrastructure/langgraph/__init__.py
+ src/app/infrastructure/langgraph/checkpointer.py
+ scripts/checkpointer_setup.py
+ migrations/versions/0010_langgraph_schema.py
+ tests/integration/test_checkpointer.py
~ pyproject.toml                          # langgraph, langgraph-checkpoint-postgres
~ src/app/infrastructure/postgres/grants.py
~ src/app/config/settings.py              # PostgresSettings.checkpointer_dsn()
~ Makefile                                # migrate runs checkpointer_setup
~ tests/unit/test_project_layout.py       # the import-boundary assertion
```

## Schema (migration `0010_langgraph_schema`)

`down_revision = "0009"`. The whole migration is three statements and no tables:

```sql
CREATE SCHEMA IF NOT EXISTS langgraph;
GRANT USAGE ON SCHEMA langgraph TO gym_workflow_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA langgraph
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gym_workflow_worker;
```

`ALTER DEFAULT PRIVILEGES` is the point: `setup()` creates the tables *after* this migration runs, and a future checkpointer version that adds a fifth table must not require a migration to become writable. `downgrade()` drops the schema with `CASCADE` — checkpoints are rebuildable coordination state, not history.

**DELETE is granted here and nowhere else in the system.** §26's soft-deletion rule is about domain rows; a checkpointer that cannot delete its own superseded writes leaks unboundedly. The invariant test in `tests/unit/test_grants.py` asserts "no service has DELETE" over `ALL_TABLES`, which is the `public` schema — state that in a comment beside the grant, or the next reader will think the rule was quietly broken.

## Signatures

```python
# infrastructure/langgraph/checkpointer.py
CHECKPOINT_SCHEMA: Final = "langgraph"
CHECKPOINT_TABLES: Final = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")

class CheckpointerProvider:
    """Owns the psycopg pool the checkpointer writes on.

    Deliberately not the SQLAlchemy engine: the checkpointer commits on its own
    connection, in its own transaction, and pretending otherwise would hide the
    seam that WS-7 has to prove is safe.
    """
    def __init__(self, dsn: str, *, max_size: int = 4) -> None: ...
    async def __aenter__(self) -> AsyncPostgresSaver: ...
    async def __aexit__(self, *exc: object) -> None: ...

def checkpointer_dsn(postgres: PostgresSettings, role: PostgresRole) -> str:
    """A psycopg (not asyncpg) DSN whose search_path is the checkpoint schema."""
```

```python
# scripts/checkpointer_setup.py
async def main() -> None:
    """Create the checkpoint tables under the admin identity. Idempotent:
    the checkpointer keeps its own `checkpoint_migrations` ledger."""
```

## Tests

`tests/integration/test_checkpointer.py`

- `test_the_checkpoint_tables_live_in_their_own_schema` — after `setup()`, all four names are in `information_schema.tables` under `langgraph` and none is under `public`. **This is the test that verifies D2 rather than trusting it.**
- `test_a_checkpoint_survives_the_process_that_wrote_it` — write through one provider, close it, build a second from a fresh pool, read the same `thread_id` back.
- `test_the_worker_role_can_write_a_checkpoint` — as `gym_workflow_worker`, insert and read.
- `test_the_worker_role_cannot_create_a_table_there` — as `gym_workflow_worker`, `CREATE TABLE langgraph.x(...)` raises. On its own connection: an aborted transaction makes every later statement fail for the wrong reason (Sprint 2, WS-6).
- `test_a_table_created_later_is_writable_without_a_migration` — create one as admin, then write to it as the worker. This is what `ALTER DEFAULT PRIVILEGES` buys.

`tests/unit/test_project_layout.py`

- `test_only_the_graph_package_imports_langgraph` — walk `src/app`, parse imports, assert `langgraph` and `langchain` appear only under `app/graphs/` and `app/infrastructure/langgraph/`.

## Done when

`make migrate` creates the schema and the tables, `make check` is green, and the four checkpoint tables are provably absent from `public`.

---

# WS-2 — Workflow operational tables

**Branch:** `feat/ws-2-workflow-ops-tables`

## Files

```text
+ migrations/versions/0011_workflow_operations.py
+ tests/integration/test_workflow_operations.py
~ src/app/infrastructure/postgres/models.py     # ExecutionTask, PendingClarification, enums
~ src/app/infrastructure/postgres/grants.py
~ src/app/config/settings.py                    # clarification_timeout
~ .env.example
~ tests/conftest.py                             # TRUNCATE list
~ tests/unit/test_persistence_contracts.py      # EXPECTED_TABLES
```

## Domain enums

```python
class TaskStatus(StrEnum):
    PENDING = "pending"; READY = "ready"; RUNNING = "running"
    COMPLETED = "completed"; FAILED = "failed"
    WAITING_FOR_USER = "waiting_for_user"; SKIPPED = "skipped"

class DependencyPolicy(StrEnum):
    REQUIRE_SUCCESS = "require_success"; ALLOW_PARTIAL = "allow_partial"; OPTIONAL = "optional"

class ClarificationStatus(StrEnum):
    WAITING = "waiting"; ANSWERED = "answered"; CANCELLED = "cancelled"; EXPIRED = "expired"

class ClarificationReason(StrEnum):
    MISSING_ESSENTIAL_DATA = "missing_essential_data"; AMBIGUOUS_ENTITY = "ambiguous_entity"
```

`WorkflowExecutionStatus` gains `WAITING_FOR_USER` and `PARTIAL_SUCCESS` (Q28).

`ClarificationReason` deliberately mirrors a *subset* of `DeferralReason`: not every deferral becomes a question (an `INVALID_VALUE` on an otherwise complete activity is answered by asking again, not by suspending a workflow). Map explicitly in WS-8; do not reuse the enum.

## Schema (migration `0011_workflow_operations`)

`down_revision = "0010"`.

```text
execution_tasks
  id                    uuid pk
  workflow_execution_id uuid not null fk -> workflow_executions(id) on delete cascade
  task_key              text not null          -- stable within a plan; the plan's own id
  task_type             varchar(64) not null   -- TaskType
  status                varchar(64) not null check
  result_visibility     varchar(32) not null check
  depends_on            jsonb not null default '[]'  -- [{task_key, policy}]
  payload               jsonb not null default '{}'
  result_facts          jsonb null
  error                 text null
  attempts              integer not null default 0
  started_at            timestamptz null
  finished_at           timestamptz null
  created_at            timestamptz not null default now()
  updated_at            timestamptz not null default now()
  UNIQUE (workflow_execution_id, task_key)
  CHECK (status IN ('completed','failed','skipped')) = (finished_at IS NOT NULL)

pending_clarifications
  id                       uuid pk
  clarification_id         text not null unique     -- what the ClarificationSpec carries
  workflow_execution_id    uuid not null fk -> workflow_executions(id) on delete cascade
  user_id                  uuid not null fk -> users(id) on delete cascade
  conversation_id          uuid not null fk -> conversations(id) on delete cascade
  task_key                 text not null
  reason                   varchar(64) not null check
  status                   varchar(64) not null check
  spec                     jsonb not null           -- the frozen ClarificationSpec
  expires_at               timestamptz not null
  created_at               timestamptz not null default now()
  updated_at               timestamptz not null default now()
  resolved_at              timestamptz null
  answer_message_batch_id  uuid null fk -> message_batches(id) on delete set null
  FOREIGN KEY (workflow_execution_id, task_key) -> execution_tasks(workflow_execution_id, task_key)
  UNIQUE INDEX uq_pending_clarifications_open_per_conversation
         ON (conversation_id) WHERE status = 'waiting'
  CHECK ((status = 'waiting') = (resolved_at IS NULL))
```

Two details that will be got wrong if they are not written down:

- The composite FK to `execution_tasks(workflow_execution_id, task_key)` needs a `UNIQUE` on exactly that pair to reference — Alembic **will not** emit it from a `UniqueConstraint` declared only in `__table_args__` if a same-column index already exists. Sprint 2's WS-6 lost an afternoon to precisely this; write it into the migration by hand and assert the constraint exists.
- The status CHECKs must be **named explicitly** (`ck_execution_tasks_status`, `ck_pending_clarifications_status`, `ck_pending_clarifications_reason`). Two auto-named CHECKs on one table collide.

Both tables inherit `Base`, which maps `id`, `created_at` **and** a non-null
`updated_at` with an `onupdate` (`src/app/infrastructure/postgres/base.py`). A
migration that stops at `created_at` produces an ORM that selects a column the
database does not have — write `updated_at` into both `create_table` calls.

`workflow_executions` changes in three ways, not one:

```python
# 1. the status vocabulary (Q28)
op.drop_constraint("workflowexecutionstatus", "workflow_executions", type_="check")
op.create_check_constraint(
    "ck_workflow_executions_status", "workflow_executions",
    "status IN ('running','succeeded','failed','waiting_for_user','partial_success')")

# 2. terminality, which the sprint file promises and the enum CHECK does not give
op.create_check_constraint(
    "ck_workflow_executions_finished_when_terminal", "workflow_executions",
    "(status IN ('succeeded','failed','partial_success')) = (finished_at IS NOT NULL)")

# 3. Q132: the version of the graph that produced this execution
op.add_column("workflow_executions",
              sa.Column("graph_version", sa.String(32), nullable=True))
```

`waiting_for_user` is deliberately **not** terminal: the execution is paused,
not finished, and stamping `finished_at` on it would make "how long do users
wait for an answer" unanswerable.

`graph_version` is nullable because rows written before this migration have no
honest value for it; WS-7 populates it on every new execution. It is a column
rather than a checkpoint field for the reason ADR-015 states — the checkpoint is
not authoritative and may be pruned, so traceability that lives only there is
traceability that disappears.

`downgrade()` drops both tables, both new constraints and the column, and
restores the three-value constraint — which fails loudly if a `waiting_for_user`
row exists, and that is the correct behaviour: a downgrade that silently
discards a user's open question is worse than one that refuses.

## Grants

`ServiceName.WORKFLOW_WORKER`: `execution_tasks` `(SELECT, INSERT, UPDATE)`, `pending_clarifications` `(SELECT, INSERT, UPDATE)`. `ServiceName.SESSION_EXPIRATION_WORKER`: `pending_clarifications` `(SELECT, UPDATE)` — it closes expired questions and writes nothing else. No role gets DELETE: a clarification is resolved, never erased, because "what was the system waiting for when this went wrong" is the question these rows exist to answer.

## Tests

`tests/integration/test_workflow_operations.py`

- `test_one_open_question_per_conversation` — a second WAITING insert raises `IntegrityError`; the same conversation accepts a new one once the first is ANSWERED. Asserts the partial index, not application code.
- `test_a_finished_task_must_say_when` — COMPLETED with `finished_at` NULL is refused.
- `test_a_clarification_belongs_to_a_task_of_its_own_execution` — the composite FK refuses a `task_key` from another execution.
- `test_the_new_workflow_statuses_are_accepted` — `waiting_for_user` and `partial_success` insert; `nonsense` is refused.
- `test_a_terminal_execution_must_say_when_it_finished` — `succeeded` with `finished_at` NULL is refused, and `waiting_for_user` with `finished_at` NULL is accepted, because a paused execution has not finished.
- `test_an_execution_records_the_graph_that_produced_it` — `graph_version` is present on every row WS-7 writes.
- `test_no_role_can_delete_a_clarification` — per role, fresh connection each.

`tests/unit/test_persistence_contracts.py` — `EXPECTED_TABLES` grows by two and still equals `ALL_TABLES`.

## Done when

`make migrate` applies and reverses cleanly on a populated database, and the suite is green with the two new tables in every list.

---

# WS-3 — ExecutionPlan as data

**Branch:** `feat/ws-3-execution-plan`

Pure domain. No SQLAlchemy, no LangGraph, no I/O — the layer test in `tests/unit/test_project_layout.py` already enforces the first, and WS-1 added the second.

## Files

```text
+ src/app/domain/workflow/__init__.py
+ src/app/domain/workflow/plan.py
+ tests/domain/test_execution_plan.py
+ tests/domain/fixtures/execution_plans.json
```

## Signatures

```python
# domain/workflow/plan.py

@dataclass(frozen=True, slots=True)
class Dependency:
    task_key: str
    policy: DependencyPolicy = DependencyPolicy.REQUIRE_SUCCESS

@dataclass(frozen=True, slots=True)
class PlannedTask:
    key: str
    task_type: TaskType
    result_visibility: ResultVisibility
    payload: Mapping[str, str] = field(default_factory=dict)
    depends_on: tuple[Dependency, ...] = ()
    status: TaskStatus = TaskStatus.PENDING

@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """§11.2. Immutable: every transition returns a new plan.

    `can_run_parallel` is deliberately absent. Parallelism is a *derivation*
    from `depends_on`, and storing it would create a second source of truth
    that can disagree with the first.
    """
    tasks: tuple[PlannedTask, ...]

    def __post_init__(self) -> None:
        """Refuses: an unknown dependency key, a duplicate key, and a cycle.
        A cycle discovered at scheduling time is an infinite loop; discovered
        here it is a `PlanCycleError` with the offending keys named."""

    def ready(self) -> tuple[PlannedTask, ...]: ...
    def started(self, key: str) -> ExecutionPlan: ...
    def completed(self, key: str, *, facts: Mapping[str, str]) -> ExecutionPlan: ...
    def failed(self, key: str, *, error: str) -> ExecutionPlan: ...
    def waiting(self, key: str) -> ExecutionPlan: ...
    def is_terminal(self) -> bool:
        """No task is PENDING, READY or RUNNING. WAITING_FOR_USER is terminal
        for this delivery -- the workflow ends and resumes on another message."""
    def outcome(self) -> WorkflowOutcomeStatus:
        """SUCCEEDED | PARTIAL_SUCCESS | FAILED | WAITING_FOR_USER (Q28)."""
```

Skipping is transitive and policy-aware, and this is the part worth reviewing carefully:

```text
failed(A):
  for each task T that depends on A:
    REQUIRE_SUCCESS -> T becomes SKIPPED, and the same rule applies to T's dependants
    ALLOW_PARTIAL   -> T stays schedulable; A's absence is T's problem to handle
    OPTIONAL        -> T stays schedulable, unchanged
```

`SKIPPED` is not `FAILED`. A skipped task never ran, so it has no error and must not be reported to the user as a failure; `outcome()` treats a plan with skipped-but-no-failed tasks as `PARTIAL_SUCCESS`, not `FAILED`.

## Tests

`tests/domain/test_execution_plan.py`

- `test_independent_tasks_are_ready_together` — a plan of two roots yields both in one `ready()`.
- `test_a_diamond_schedules_in_two_waves` — A → (B, C) → D: `ready()` returns A; then B and C together; then D only after both.
- `test_a_required_predecessor_that_fails_skips_the_whole_subtree` — the grandchild is SKIPPED too.
- `test_a_skipped_task_is_not_a_failed_task` — `outcome()` is `PARTIAL_SUCCESS`, and the skipped task carries no error.
- `test_allow_partial_runs_anyway` / `test_optional_never_blocks`.
- `test_a_cycle_is_refused_at_construction` — `PlanCycleError` naming both keys.
- `test_an_unknown_dependency_is_refused` — a typo in a `task_key` fails immediately rather than deadlocking `ready()` forever.
- `test_a_plan_is_terminal_only_when_nothing_can_run`.
- `test_a_waiting_task_ends_this_delivery` — `is_terminal()` is True, `outcome()` is `WAITING_FOR_USER`.
- `test_the_golden_plans_walk_the_same_way` — `execution_plans.json` holds three plans with their expected wave-by-wave schedule and final outcome. The fixture is the regression net for the day someone "simplifies" the skip rule.

## Done when

The engine is exercised without a database, a graph or a mock, and the golden file replays.

---

# WS-4 — MainGraph, the static topology

**Branch:** `feat/ws-4-main-graph`

## Files

```text
+ src/app/graphs/main/state.py
+ src/app/graphs/main/graph.py
+ src/app/graphs/main/nodes.py
+ src/app/application/ports/routing.py          # IntentRouterPort, ExecutionPlannerPort
+ src/app/application/services/execution_planner.py
+ tests/graph/__init__.py
+ tests/graph/test_main_graph_topology.py
+ tests/graph/test_main_graph_run.py
~ src/app/graphs/main/routing.py                # route() wrapped by DeterministicIntentRouter
```

## Signatures

```python
# graphs/main/state.py  -- §11.4, and nothing beyond it

GRAPH_VERSION: Final = "main.v1"

class MainGraphState(TypedDict, total=False):
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
    task_results: Annotated[dict[str, TaskExecutionResult], merge_task_results]
    pending_interrupt: ClarificationSpec | None
    response_input: tuple[DomainResult, ...]
    outbound_messages: tuple[OutboundText, ...]
    workflow_errors: tuple[str, ...]
```

Q27 is a constraint on this type and the review question for every field added later: *is this a reference, or a copy of the database?* No message rows, no user profile, no history. `merge_task_results` is the reducer that makes the fan-in safe — two tasks completing concurrently must not clobber each other's entry.

```python
# graphs/main/graph.py

def build_main_graph(
    *,
    router: IntentRouterPort,
    planner: ExecutionPlannerPort,
    handlers: Mapping[TaskType, TaskHandler],
    normalizer: ResponseNormalizerPort,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    """Compiled once, at process start (Q121)."""

def thread_for(conversation_id: UUID) -> dict[str, Any]:
    """`{"configurable": {"thread_id": str(conversation_id)}}` (Q123, D4)."""
```

```python
# application/ports/routing.py

class IntentRouterPort(Protocol):
    async def route(self, texts: Sequence[str]) -> tuple[TaskType, ...]: ...

class ExecutionPlannerPort(Protocol):
    async def plan(self, intents: Sequence[TaskType], *, texts: Sequence[str]) -> ExecutionPlan: ...
```

Both are `async` although this sprint's implementations do no I/O. A synchronous port would force every call site to change when Sprint 4 puts a network call behind it, which is exactly the rewrite these ports exist to prevent.

```python
# graphs/main/nodes.py -- one function per §11.1 node

async def load_base_context(state, runtime) -> dict: ...      # identity, session, pending state
async def resolve_pending_workflow(state, runtime) -> dict: ...  # WS-9 fills this; identity here
async def normalize_input(state, runtime) -> dict: ...        # identity this sprint, asserted
async def intent_router(state, runtime) -> dict: ...
async def execution_planner(state, runtime) -> dict: ...
async def collect_results(state, runtime) -> dict: ...        # WS-6
async def response_normalizer(state, runtime) -> dict: ...    # WS-6
async def response_guard(state, runtime) -> dict: ...         # WS-6
async def persist_outbound(state, runtime) -> dict: ...       # WS-7
```

The database session is **not** in graph state. It reaches the nodes through the runtime context the worker supplies, because a session in a checkpointed state object would be serialized, and a serialized session is a crash at resume time.

## Tests

`tests/graph/test_main_graph_topology.py`

- `test_the_graph_has_exactly_the_nodes_the_spec_lists` — set equality against a literal copy of §11.1's list. Structural, so an added node is a failing test and a deliberate decision.
- `test_the_edges_match_the_spec` — the edge set, likewise. This is the test that catches a "quick fix" edge added to route around a bug.
- `test_the_graph_is_compiled_once` — building the app twice returns the same compiled object; a counter on the builder proves it is not per-message (Q121).
- `test_the_graph_version_is_recorded` — every run writes `GRAPH_VERSION` into state (Q132).

`tests/graph/test_main_graph_run.py`

- `test_a_conversation_message_walks_the_whole_graph` — fakes for every port, an in-memory checkpointer, no database. Asserts the node visit order.
- `test_normalize_input_is_identity_this_sprint` — the deliberate deviation, asserted so removing it later is visible.
- `test_the_state_carries_references_not_records` — the state's keys equal §11.4's list. Q27 enforced by a test rather than by good intentions.

## Done when

The graph compiles at import of the entrypoint, runs end to end against fakes, and its shape is asserted against the spec rather than against itself.

---

# WS-5 — Task scheduler, fan-out and the result reducer

**Branch:** `feat/ws-5-task-scheduler`

## Files

```text
+ src/app/graphs/main/scheduler.py
+ src/app/infrastructure/postgres/execution_tasks.py
+ tests/graph/test_task_scheduler.py
+ tests/integration/test_execution_tasks.py
~ src/app/graphs/main/graph.py
~ src/app/graphs/main/nodes.py
```

## Signatures

```python
# graphs/main/scheduler.py

MAX_SCHEDULING_PASSES: Final = 64

async def task_scheduler(state, runtime) -> Command:
    """Send every READY task, then reduce results, until the plan is terminal.

    The bound is not decoration: a plan whose `ready()` returns nothing while
    tasks remain PENDING is a bug in WS-3, and without a bound it presents as a
    worker that consumes a partition forever and never acks.
    """

async def run_task(task: PlannedTask, runtime) -> TaskExecutionResult: ...
```

```python
# infrastructure/postgres/execution_tasks.py

async def record_plan(session, *, execution_id: UUID, plan: ExecutionPlan) -> None:
    """Insert one row per task, ON CONFLICT DO NOTHING on
    (workflow_execution_id, task_key) -- a redelivery re-plans and must not
    collide with the rows the first delivery wrote."""

async def record_transition(session, *, execution_id: UUID, task: PlannedTask,
                            facts: Mapping[str, str] | None = None,
                            error: str | None = None) -> None: ...
```

`execution_tasks` is written from the same session as the domain work, in the same transaction (Q128 wants operational visibility, not a second consistency domain). It is therefore only visible after the commit — say so in the module docstring, because "why is `execution_tasks` empty while the workflow is running" is otherwise a debugging session someone will have.

## Tests

`tests/graph/test_task_scheduler.py`

- `test_two_independent_tasks_run_from_one_pass`.
- `test_a_dependant_runs_only_after_its_dependency_completed`.
- `test_a_failing_handler_marks_its_task_failed_and_keeps_going` — the independent sibling still COMPLETED, the plan's outcome `PARTIAL_SUCCESS`.
- `test_a_failing_handler_does_not_kill_the_workflow` — no exception escapes the scheduler; the error is recorded.
- `test_the_scheduler_is_bounded` — a plan rigged to never progress raises rather than spinning.
- `test_concurrent_results_do_not_clobber_each_other` — two tasks resolving at once, both present in `task_results` (the reducer).

`tests/integration/test_execution_tasks.py`

- `test_every_task_reaches_a_terminal_row` — after a run, each task has a row with a terminal status and a `finished_at`.
- `test_the_rows_tell_the_same_story_as_the_plan` — statuses in the table equal statuses in the final plan. Two sources of truth that can disagree are worth one test that says they do not.
- `test_a_redelivery_does_not_duplicate_task_rows` — the `ON CONFLICT`.

## Done when

A multi-task plan runs, its outcome is right, and the table can answer what happened without opening a checkpoint.

---

# WS-6 — Result collection, the response boundary and the guard

**Branch:** `feat/ws-6-response-guard`

## Files

```text
+ src/app/domain/response/__init__.py
+ src/app/domain/response/guard.py
+ src/app/application/ports/response.py
+ src/app/application/services/response_normalizer.py
+ tests/domain/test_response_guard.py
+ tests/graph/test_response_boundary.py
~ src/app/graphs/main/nodes.py
```

## Signatures

```python
# domain/response/guard.py  -- pure, and the whole point of DEC-004

@dataclass(frozen=True, slots=True)
class GuardViolation:
    fact: str
    expected: str
    reason: Literal["missing", "altered"]

def verifiable_facts(results: Sequence[DomainResult]) -> Mapping[str, str]:
    """The facts a normalizer is forbidden to change: exercise names, loads,
    repetition counts, RPE, set counts. Taken from `DomainResult.facts`, which
    Sprint 2 already populates -- this is why that field exists."""

def check(text: str, facts: Mapping[str, str]) -> tuple[GuardViolation, ...]:
    """Every fact must appear in the text, unaltered. Numbers are compared as
    numbers: "3 séries" satisfies `sets=3`, "três séries" does not, and
    "4 séries" is a violation rather than a stylistic choice."""
```

```python
# application/ports/response.py
class ResponseNormalizerPort(Protocol):
    async def normalize(self, results: Sequence[DomainResult]) -> tuple[OutboundText, ...]: ...

# application/services/response_normalizer.py
class TemplateResponseNormalizer:
    """This sprint's implementation: the pt-BR templates Sprint 2 wrote,
    ordered. It cannot lie, which is exactly why the guard's tests must use a
    fake that can."""
```

Fallback behaviour (§25): when `check()` returns violations, the guard logs them at WARNING with the fact names, discards the normalizer's text, and emits the deterministic template built straight from the `DomainResult`. **A guard violation never silences the confirmation** — Q12 requires every successful registration to be confirmed, so failing closed here would trade a wrong number for a lost acknowledgement.

## Tests

`tests/domain/test_response_guard.py`

- `test_a_dropped_load_is_a_violation` / `test_an_altered_repetition_count_is_a_violation`.
- `test_a_reworded_sentence_with_every_fact_intact_passes` — the guard must not become a template-equality check, or Sprint 4's normalizer can never pass it.
- `test_numbers_are_compared_as_numbers` — `sets=3` against "3 séries" passes, against "4 séries" fails.
- `test_an_internal_result_contributes_facts_and_no_prose`.

`tests/graph/test_response_boundary.py`

- `test_a_lying_normalizer_is_overruled` — a fake normalizer that halves the load; the user receives the deterministic fallback and the violation is logged. **The guard is proven to fail, not only asserted to pass.**
- `test_the_confirmation_still_arrives_when_the_guard_fires` — Q12.
- `test_two_visible_results_keep_their_plan_order` — sequences 0 and 1, in plan order.
- `test_partial_success_says_both_things` — one committed task and one failed task produce a reply naming what was recorded and what was not.

## Done when

A normalizer cannot change a fact and reach a user, and a test proves it by trying.

---

# WS-7 — The worker runs the graph

**Branch:** `feat/ws-7-worker-runs-the-graph`

The riskiest merge in the sprint: everything Sprint 1 and Sprint 2 verified must keep holding while the machinery underneath is replaced.

## Files

```text
~ src/app/workers/workflow_worker.py
~ src/app/entrypoints/workflow_worker.py       # compile once, own the checkpointer's lifetime
+ tests/integration/test_workflow_worker_graph.py
+ tests/e2e/test_checkpoint_failure_injection.py
```

## Signatures

```python
class WorkflowWorker:
    def __init__(self, *, session_factory, graph: CompiledStateGraph, settings) -> None: ...

    async def handle(self, body: dict[str, Any]) -> WorkflowOutcome:
        """One InputBatchReady in, one response group out.

        Unchanged from Sprint 1 and re-asserted rather than assumed:
        `workflow_executions`, `outbound_messages`, `domain_events` and
        `outbox_events` commit together; the ACK follows that commit and not
        the provider's delivery (Q130); a redelivered batch that already
        SUCCEEDED returns without effects.

        Changed: the routing and handler call become `await graph.ainvoke(
        initial_state, config=thread_for(batch.conversation_id))`.
        """
```

`WorkflowOutcome` gains `status: WorkflowOutcomeStatus` so a caller can tell SUCCEEDED from PARTIAL_SUCCESS from WAITING_FOR_USER without re-reading the database.

## The seam, stated plainly

The checkpointer commits on a psycopg connection; the domain commits on ours. There is a window in which a checkpoint exists for a domain transaction that then rolled back. This is **not** fixed by ordering, and the plan does not pretend otherwise. It is survivable because of what Sprint 2 already built:

- `processed_operations` is the authority on whether a business effect happened. A resumed graph that re-runs a command finds the claim taken and writes nothing.
- The checkpoint is coordination state. Being ahead of the domain costs a redundant node execution, never a duplicate row.

WS-7 does not argue this. It injects the failure and counts rows.

## Tests

`tests/integration/test_workflow_worker_graph.py`

- The whole of Sprint 1's and Sprint 2's worker suites, still green, with **no assertion relaxed**. If a test needs changing to accommodate the graph, that is a finding for the PR description, not a quiet edit.
- `test_the_thread_is_the_conversation` — the checkpoint lands under `str(conversation_id)` (Q123).
- `test_two_batches_in_one_conversation_share_a_thread` — and the second does not resume the first.
- `test_a_graph_that_raises_fails_the_execution` — status FAILED, error recorded, the exception propagates so the broker retries.

`tests/e2e/test_checkpoint_failure_injection.py`

- `test_a_checkpoint_ahead_of_a_rollback_still_yields_one_set` — inject a failure after the checkpoint write and before the domain commit; redeliver; assert exactly one set, one audit row, one outbound message.
- `test_a_crash_between_commit_and_ack_does_not_double_the_reply` — Sprint 1's guarantee, re-proven through the graph.

## Done when

`make demo` behaves identically to Sprint 2 — the user-visible behaviour of this workstream is *nothing* — and the failure-injection suite passes.

---

# WS-8 — Clarification interrupts

**Branch:** `feat/ws-8-clarification-interrupts`

## Files

```text
+ src/app/domain/clarification/__init__.py
+ src/app/domain/clarification/spec.py
+ src/app/domain/clarification/questions.py
+ src/app/graphs/main/interrupts.py
+ src/app/infrastructure/postgres/clarifications.py
+ tests/contract/fixtures/clarification_spec.json
+ tests/contract/test_clarification_spec.py
+ tests/integration/test_clarification_interrupt.py
~ src/app/graphs/main/handlers.py
~ src/app/workers/workflow_worker.py            # completes the interrupt: row, question, status
~ src/app/application/commands/workout.py       # operation_id_for_clarification
```

## The contract

```python
# domain/clarification/spec.py  -- §40.2, frozen by a golden fixture

SCHEMA_VERSION: Final = "clarification-spec.v1"

class MissingField(BaseModel, frozen=True, extra="forbid"):
    activity_ref: str          # which activity in the batch
    field: ActivityField       # repetitions, load, duration...
    raw_name: str              # what the user called the exercise

class AmbiguousEntity(BaseModel, frozen=True, extra="forbid"):
    activity_ref: str
    raw_name: str
    candidates: tuple[Candidate, ...]   # exercise_id + canonical_name + confidence

class ClarificationSpec(BaseModel, frozen=True, extra="forbid"):
    schema_version: Literal["clarification-spec.v1"]
    clarification_id: str
    reason: ClarificationReason
    original_task_key: str
    missing_fields: tuple[MissingField, ...] = ()
    ambiguous_entities: tuple[AmbiguousEntity, ...] = ()
    expected_response_schema: ExpectedResponse
```

`ExpectedResponse` is the field that makes WS-9 possible and §40.2 does not describe, so it is defined here and recorded in ADR-016:

```python
class ExpectedResponse(BaseModel, frozen=True, extra="forbid"):
    kind: Literal["integer_list", "decimal", "choice"]
    #: For "choice": the candidate names an answer may select, in the order
    #: they were offered, so "o primeiro" and the name itself both resolve.
    options: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
```

A question the system cannot recognise the answer to is a question it should not ask. `kind` is what turns "8 8 8" into an answer rather than an unparseable message, and it is chosen at the moment the question is asked, by the code that knows what it was asking for.

## Where the interrupt goes

```python
# graphs/main/interrupts.py  -- the only module in the repo that imports interrupt()

def ask(spec: ClarificationSpec) -> ClarificationAnswer:
    """Suspend this workflow until the user answers. Returns the parsed answer
    when the graph is resumed with Command(resume=...)."""
```

The LOG_WORKOUT handler changes shape (§15, Q56, Q126):

```text
build the command from the batch
  -> if there are valid activities:
         commit them          # operation_id_for(message_batch_id), unchanged
  -> if there are deferrals that are answerable:
         ask(spec)            # <- the interrupt; nothing after this line runs now
  -> confirmation naming what was written and what is still open
```

**`ask()` ends the delivery, and that is the trap.** `interrupt()` suspends the
whole graph: `collect_results`, `response_normalizer` and `persist_outbound`
never run for this message. A plan that leaves the question to "the normal
outbound path" therefore ships a system that suspends a workflow and tells the
user nothing — the one failure mode this sprint exists to remove.

So the interrupt is completed **by the worker, outside the paused graph**, in the
transaction it already owns (WS-7):

```text
state = await graph.ainvoke(initial_state, config=thread_for(conversation_id))

if interrupt_of(state) is not None:          # LangGraph reports the pending interrupt
    spec = interrupt_of(state)
    write pending_clarifications(spec)       # WAITING, expires_at = now + D10
    write outbound_messages(question_for(spec))   # the same response-group path
    execution.status = WAITING_FOR_USER
    # commit, then ACK -- unchanged from Q130
```

Three consequences worth stating, because each is a test below:

- The question text is built by `question_for(spec)` in
  `domain/clarification/questions.py` — a pure function, reusing Sprint 2's
  `clarification_request()` phrasing so the user sees the same sentences as today.
- It goes through `outbound_messages` and the outbox like every other reply, so
  the dispatcher, the retry tiers and the ordering guarantee all still apply.
  Nothing about the outbound path is special-cased for clarifications.
- The `pending_clarifications` row and the outbound question commit **together**.
  A question the user received with no row to answer against, or a row with no
  question sent, are both worse than neither.

`interrupt_of(state)` is the one place the plan touches LangGraph's interrupt
representation, and it is deliberately one function: the exact shape of what
`ainvoke` returns for a suspended graph is on the *unmeasured* list at the top
of this file. Measure it in WS-4 and keep the coupling to a single accessor.

Two further rules that the tests exist to pin down:

- **Commit before interrupting, never after.** Q56 requires the valid activities to be persisted, and Q126 requires the interrupt to precede the *dependent* side effect — the deferred item's rows, which is exactly what is not written yet. The clarification row and the question are not dependent side effects; they are how the pause becomes visible.
- **The resumed write uses a different idempotency key.** `operation_id_for(batch)` is already claimed by the commit above. The resumed command uses `operation_id_for_clarification(clarification_id)`, so resuming cannot collide with, or be swallowed by, the claim the first delivery took.

`ClarificationReason` is mapped from `DeferralReason` explicitly:

| `DeferralReason` | Becomes |
| --- | --- |
| `MISSING_ESSENTIAL_DATA` | `ClarificationReason.MISSING_ESSENTIAL_DATA`, `kind="integer_list"` or `"decimal"` by field |
| `AMBIGUOUS_EXERCISE` | `ClarificationReason.AMBIGUOUS_ENTITY`, `kind="choice"` with the candidates |
| `UNRESOLVED_EXERCISE` | `ClarificationReason.AMBIGUOUS_ENTITY`, `kind="choice"` with no options — a free-text name is accepted |
| `INVALID_VALUE` | **No interrupt.** Asked and answered in one message, as Sprint 2 does today |

## Tests

`tests/contract/test_clarification_spec.py`

- `test_the_golden_spec_round_trips` — byte-identical, unknown fields refused, `schema_version` pinned. Sprint 4 gets a contract, not an example.

`tests/integration/test_clarification_interrupt.py`

- `test_an_incomplete_workout_writes_nothing_and_asks` — zero `exercise_sets`, one WAITING row, one outbound question, execution `WAITING_FOR_USER`.
- `test_a_mixed_batch_keeps_what_it_understood` — the valid exercise is persisted, the ambiguous one is not, and the reply says both (Q56, Q57).
- `test_the_interrupt_precedes_the_deferred_write` — asserted on row counts at the moment of suspension, not by reading the code.
- `test_the_clarification_row_and_the_checkpoint_agree` — the WAITING row's `clarification_id` is the one inside the checkpointed spec.
- `test_the_question_and_its_row_commit_together` — inject a failure between the two writes; assert neither survives. A question with nothing to answer against is the worst outcome available here.
- `test_the_question_travels_the_ordinary_outbound_path` — it lands in `outbound_messages` with a response group and an outbox row, exactly like a confirmation.
- `test_a_redelivery_while_waiting_asks_once` — the same batch delivered twice produces one WAITING row and one outbound question.

## Done when

`#log supino 80kg` suspends a real workflow, and the database can say what it is waiting for without opening a checkpoint.

---

# WS-9 — Resume, and what an incoming message means

**Branch:** `feat/ws-9-resume-and-pending-workflows`

## Files

```text
+ src/app/domain/conversation/__init__.py
+ src/app/domain/conversation/cancellation.py
+ src/app/domain/clarification/answers.py
+ src/app/application/services/pending_workflows.py
+ tests/domain/test_clarification_answers.py
+ tests/integration/test_resume.py
+ tests/integration/test_learned_aliases.py
~ src/app/graphs/main/nodes.py                  # resolve_pending_workflow reads the decision
~ src/app/workers/session_expiration_worker.py  # the expiry sweep
~ src/app/workers/workflow_worker.py            # classify, then invoke or resume
```

## Signatures

```python
# domain/clarification/answers.py  -- pure

class AnswerParseError(ValueError): ...

def parse_answer(texts: Sequence[str], expected: ExpectedResponse) -> ClarificationAnswer:
    """Read the batch as an answer to this question, or raise.

    Raising is the useful outcome: a batch that does not parse is not an
    answer, and treating it as one is how "quantas repetições?" swallows
    "bom dia".
    """
```

```python
# application/services/pending_workflows.py

class InputClassification(StrEnum):
    ANSWER = "answer"; NEW_INTENT = "new_intent"; CANCELLATION = "cancellation"

@dataclass(frozen=True, slots=True)
class PendingDecision:
    classification: InputClassification
    clarification: PendingClarification | None = None
    answer: ClarificationAnswer | None = None

class PendingWorkflowResolver:
    async def classify(self, session, *, conversation_id: UUID,
                       texts: Sequence[str]) -> PendingDecision: ...
```

The order of the checks is the design, and it is deliberately not "try to parse first":

```text
1. no WAITING clarification for this conversation -> NEW_INTENT
2. it has expired                                 -> close it EXPIRED, then NEW_INTENT
3. the text is a cancel phrase                    -> CANCELLATION
4. the text carries the strict marker             -> NEW_INTENT   (Q29: a new log is not an answer)
5. parse_answer succeeds                          -> ANSWER
6. otherwise                                      -> NEW_INTENT
```

Step 4 before step 5 matters: `#log agachamento 100kg 5 5 5` contains a list of integers and *would* parse as an answer to "quantas repetições?". Ordering the marker check first is what makes Q29 hold, and the test named below is the one that fails if the two steps are ever swapped.

**The decision cannot be made inside the graph, and §11.1's node list makes it
look as though it can.** A resumed run re-enters at the interrupted node; it
never visits `resolve_pending_workflow`, which sits near the start of the graph.
A node that decides "is this an answer?" would therefore run only on the path
where the answer is *not* being applied — the classification would be correct
and useless.

So the classification runs in the **worker**, before it chooses how to invoke:

```text
decision = await resolver.classify(session, conversation_id=..., texts=...)

ANSWER        -> await graph.ainvoke(Command(resume=decision.answer),
                                     config=thread_for(conversation_id))
NEW_INTENT    -> await graph.ainvoke(initial_state | {"pending_decision": decision},
                                     config=thread_for(conversation_id))
CANCELLATION  -> close the row CANCELLED, confirm, do not touch the graph
```

`resolve_pending_workflow` **stays** as a §11.1 node and stops being identity: it
reads the decision the worker put into the initial state and records it into
`MainGraphState`, so the topology still matches the spec and the graph still
carries the fact. What it must not do is *compute* it.

This is a deviation from a literal reading of §11.1 and belongs in ADR-016 with
the reason above — the node exists, its input is computed one layer out, and
Sprint 4's harder classification (Q29's open cases) lands in the resolver
service, not in the node.

## Learned aliases

An ANSWER to an `AMBIGUOUS_ENTITY` question writes a **user-scoped row** into `exercise_aliases` — the resolver's stage 1 exists to be taught, and Sprint 2 granted the INSERT for exactly this. Written in the same transaction as the resumed workout, `ON CONFLICT DO NOTHING` on the per-user partial unique index, and never for a global alias: teaching one user's vocabulary must not edit everyone else's.

## Expiry

`session-expiration-worker` gains a second sweep: `pending_clarifications` where `status = 'waiting' AND expires_at < now()` become EXPIRED with `resolved_at` set. The checkpoint is left alone — it is garbage that costs storage, not correctness, and a retention pass is Sprint 10's business.

## Tests

`tests/domain/test_clarification_answers.py`

- `test_a_list_of_integers_answers_a_repetition_question` — "8 8 8" and "8, 8, 8" and "8 8 8 reps".
- `test_prose_does_not_answer_anything` — "bom dia" raises.
- `test_a_choice_resolves_by_name_and_by_position` — "supino reto" and "o primeiro".
- `test_a_number_outside_the_bounds_is_refused` — 900 repetitions is not an answer, it is a typo.

`tests/integration/test_resume.py`

- `test_the_round_trip_persists_on_the_second_message` — three sets after the answer, zero before, both messages present in `entity_sources`.
- `test_a_new_log_during_a_pending_question_does_not_answer_it` — the WAITING row is untouched and a second workflow runs. **The Q29 test; also the one that fails if the classifier's step order is changed.**
- `test_an_answer_after_expiry_starts_a_new_workflow` — the row is EXPIRED, nothing is resumed.
- `test_a_cancel_phrase_closes_the_question` — status CANCELLED and a confirmation the user can understand.
- `test_a_redelivered_answer_resumes_once` — one set, not two; the second delivery finds the claim taken.
- `test_the_classification_happens_before_the_graph_is_invoked` — a resumed run never visits the nodes before the interrupt, asserted on the node-visit trace. This is the test that fails if the decision is ever moved back into the graph.
- `test_the_resumed_command_uses_its_own_operation_id` — asserted on `processed_operations`, two rows with different keys.

`tests/integration/test_learned_aliases.py`

- `test_answering_which_exercise_teaches_it` — the same raw name resolves without asking on the next message.
- `test_the_alias_belongs_to_the_user_who_taught_it` — a second user still gets the question.

## Done when

The two-message exchange in the sprint file's Goal works against the running stack, and a `#log` sent in between does not disturb it.

---

# WS-10 — Dead-letter inspection and replay

**Branch:** `feat/ws-10-dead-letter-replay`

Independent of the graph work; §41 Phase 2 item 5, and the smallest thing in the sprint.

## Files

```text
+ scripts/dlq.py
+ tests/integration/test_dead_letter_replay.py
~ Makefile                                      # dlq-inspect, dlq-replay
~ src/app/infrastructure/rabbitmq/topology.py   # REPLAY_COUNT_HEADER
```

## Signatures

```python
# scripts/dlq.py
async def inspect(queue: str, *, limit: int = 20) -> list[DeadLetter]:
    """Peek without consuming: message id, routing key, death count, first
    death reason, replay count, and the first 200 bytes of the body."""

async def replay(queue: str, *, limit: int | None = None) -> int:
    """Republish to the origin queue with the original routing key and
    `x-replay-count` incremented. Returns how many were moved."""
```

A message that dead-letters twice must be visibly a repeat offender rather than looking new — otherwise a replay loop is indistinguishable from ordinary traffic in the logs.

## Tests

`tests/integration/test_dead_letter_replay.py`

- `test_an_exhausted_message_reaches_the_dlq` — through the real retry tiers, not by publishing to the DLQ directly.
- `test_inspect_does_not_consume` — listing twice returns the same messages.
- `test_replay_returns_it_to_its_origin_queue` — and the worker processes it.
- `test_replaying_twice_produces_one_business_effect` — DEC-005 through the operational path.
- `test_the_replay_count_survives` — header present and incremented.

## Done when

A dead-lettered workflow message can be seen and returned to service without anyone opening the RabbitMQ management UI.

---

# WS-11 — Cross-cutting verification

**Branch:** `feat/ws-11-cross-cutting-verification`

## Files

```text
+ tests/e2e/test_clarification_round_trip.py
~ tests/e2e/conftest.py                        # Skeleton: clarifications(), tasks(), resume helpers
~ scripts/demo.py                              # the two-message scenario
~ README.md                                    # what `make demo` now proves
```

## `Skeleton` additions

```python
async def clarifications(self, conversation_id: UUID) -> list[Row]: ...
async def execution_tasks(self, execution_id: UUID) -> list[Row]: ...
async def send_and_wait(self, text: str, *, expect_reply: bool = True) -> str: ...
```

## Tests

`tests/e2e/test_clarification_round_trip.py`

- `test_the_whole_round_trip_over_the_real_stack` — webhook, debounce, partitioned queue, graph, interrupt, checkpoint, resume, outbound. Asserts: zero sets and one WAITING row after the first message; three sets, an ANSWERED row and a confirmation naming the exercise after the second.
- `test_the_execution_tables_describe_it` — one execution per message, the first `WAITING_FOR_USER` then the resumed one `SUCCEEDED`, `execution_tasks` agreeing.
- `test_a_restart_between_the_two_messages_changes_nothing` — restart `workflow-worker` between them; the checkpoint is in PostgreSQL, so the answer still resumes. **This is what the whole checkpointer exists for; without this test the sprint has an in-memory checkpointer with extra steps.**

## `make demo`

```text
  session        01a0…
  first reply    Faltou repetições em "supino". Quanto foi?
  pending        1 waiting clarification
  sets           0
  answer         8 8 8
  second reply   Registrei Supino reto (3 séries).
  sets           3   (attributed to both messages)
```

It must **fail** when `workflow-worker` is stopped, and it must fail if the second message finds no pending clarification — the same standard Sprint 2's demo was held to.

## Done when

The demo runs green against `make up`, and red against a stack with the worker stopped.

---

# WS-12 — Decision records

**Branch:** `doc/ws-12-sprint-3-decision-records`

## Files

```text
+ doc/adr/adr-005-static-main-graph.md
+ doc/adr/adr-015-langgraph-checkpoint-isolation.md
+ doc/adr/adr-016-clarification-interrupts.md
~ doc/adr/README.md
~ tests/unit/test_decision_records.py           # EXPECTED_ADRS -> ten
~ doc/implementation-plan.md                    # sprint 3 Done, sprint 4 Planned
~ doc/sprints/sprint-03-langgraph-orchestration.md  # DoD ticked, each line naming its test
```

**ADR-005 — Static MainGraph with ExecutionPlan DAG as data.** §43 has listed it since Sprint 1 and no sprint had earned it. Traceability: §11.1, §11.2, Q121, Q122, Q127, DEC-002. Enforced by: the topology test, the compile-once test, the absence of `can_run_parallel`.

**ADR-015 — LangGraph checkpoint isolation.** The dedicated schema, `search_path` rather than a parameter that does not exist, admin-owned DDL, `thread_id = conversation_id`, and the sentence that matters most: *a checkpoint is never authoritative for business state.* Traceability: §11.5, Q123, Q124, Q145, DEC-005. Enforced by: the schema-location test, the role tests, and the checkpoint-ahead-of-rollback failure injection.

**ADR-016 — Clarification as an interrupt.** Why `pending_clarifications` exists beside the checkpoint (Q125), why at most one is open per conversation, why the marker check precedes the parse (Q29), why `expected_response_schema` had to gain a `kind` §40.2 does not name, why the pause is completed by the worker rather than by the suspended graph, and why `resolve_pending_workflow` records a decision it does not compute. Traceability: §15, §39.2, §40.2, Q29, Q125, Q126. Enforced by: the partial unique index test, the Q29 ordering test, the golden spec fixture.

## Done when

`tests/unit/test_decision_records.py` passes with ten expected records, and the sprint file's Definition of Done is ticked with a test named on every line.

---

# Definition-of-Done coverage

Every DoD line in the sprint file, and the workstream that must deliver it. A line with no workstream is a planning bug.

| DoD line | WS |
| --- | --- |
| `make demo` completes the round trip | WS-11 |
| `make check` green with containers in CI | all |
| Every workstream on its own reviewed PR | all |
| `#log supino 80kg` writes nothing and asks | WS-8 |
| `8 8 8` resumes and writes three sets | WS-9 |
| A `#log` during a pending question does not answer it | WS-9 |
| Answering *which* exercise teaches a user alias | WS-9 |
| An expired clarification is closed and not resumed | WS-9 |
| Two WAITING rows per conversation refused by the index | WS-2 |
| Nodes and edges equal §11.1 | WS-4 |
| The graph is compiled once | WS-4 |
| Checkpoint tables only in `langgraph` | WS-1 |
| The worker role writes checkpoints, creates nothing | WS-1 |
| A checkpoint ahead of a rollback yields one set | WS-7 |
| `thread_id` equals `conversation_id` | WS-4, WS-7 |
| A failed required predecessor skips its subtree | WS-3, WS-5 |
| `execution_tasks` reports terminal status | WS-5 |
| A mixed outcome ends `PARTIAL_SUCCESS` | WS-3, WS-6 |
| An altering normalizer fails the guard | WS-6 |
| `ClarificationSpec` frozen by a golden fixture | WS-8 |
| ACK after the outbound commit; no second reply | WS-7 |
| A dead letter can be listed and replayed | WS-10 |
| No domain or application module imports `langgraph` | WS-1 |
| ADR-005, ADR-015, ADR-016 committed | WS-12 |

# Sequencing risks specific to this plan

- **WS-7 is a swap under load-bearing tests.** It must not relax a single assertion from Sprint 1 or Sprint 2 to make the graph fit. If an assertion genuinely no longer applies, that belongs in the PR description as a decision, and probably in an ADR.
- **WS-8 and WS-9 are one behaviour split across two PRs.** Between them, `main` has a system that can suspend a workflow and cannot resume it — a user would be left with a question nobody can answer. Keep the interrupt behind the same condition that Sprint 2 already deferred on, so the worst case on `main` between the two merges is today's behaviour: the question is asked and the item is dropped.
- **The unmeasured items at the top of this file are load-bearing.** If `Command(resume=...)` re-runs nodes before the interrupt, WS-9's design changes materially, and finding that out during WS-9 is late. Measure it during WS-4, when the graph first runs, and record the result in that PR.
- **Migration `0011` alters an existing CHECK constraint by its auto-generated name.** Confirm the name against a live database (`\d workflow_executions`) before writing the migration, not after CI fails.
- **The scheduler's bound (`MAX_SCHEDULING_PASSES`) is a safety net, not a limit on real plans.** If a legitimate plan ever approaches it, the plan is wrong, not the bound — resist raising it.
