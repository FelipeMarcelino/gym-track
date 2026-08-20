# Sprint 3 — LangGraph Orchestration and Resumable Clarification

**Status:** Planned
**Spec basis:** v1.1 — §11 (all), §12 (shape only), §15 (the interrupt half), §25 (the guard half), §26 (workflow ops tables), §29, §39.2, §40.1, §40.2, §41 Phase 2 items 1-3 and 5
**Decision records exercised:** DEC-001, DEC-002, DEC-004 (partially), DEC-005, DEC-015
**Depends on:** Sprint 2 — the deterministic workout domain, merged and running (`make up`, `make demo`)

## Goal

Sprint 2 taught the pipeline to understand **one message**. Sprint 3 teaches it
to hold **one interaction across several messages** — still without a single
LLM call:

```text
#log supino 80kg   ->  Faltou repetições em "supino". Quanto foi?
                       workflow_executions.status = WAITING_FOR_USER
                       pending_clarifications: 1 row, WAITING
                       exercise_sets: 0 rows

8 8 8              ->  Registrei Supino reto (3 séries).
                       the same workflow, resumed from its checkpoint
```

Today the first message produces that question and then throws the workout
away. Nothing remembers that a question was asked, so the answer is just an
unparseable message. After this sprint the workflow **pauses** — a checkpoint in
PostgreSQL, a row in `pending_clarifications`, an execution that says
`WAITING_FOR_USER` — and the next message resumes it where it stopped.

The second half of the sprint is structural. The workflow worker stops calling
a handler by hand and starts running a **static, compiled MainGraph** whose work
is described by an **ExecutionPlan that is data, not topology** (DEC-002, Q121).
Task status leaves the graph state and becomes a queryable table (Q118, Q128).
Every reply passes a **ResponseGuard** that compares the prose against the facts
the domain produced (Q62, Q136).

**Zero LLM in this sprint.** This is the same ordering invariant Sprint 2 was
built on, applied one level up: an interrupt that resumes to the wrong node, a
dependency policy that runs a task it should have skipped, or a checkpoint
written for a transaction that rolled back are all bugs that a probabilistic
component upstream would make nearly impossible to localise. They are cheap to
falsify while every input is deterministic.

**Demo at end of sprint:** the two-message exchange above, followed by proof
that a redelivery of either message changes nothing, and that a `#log` sent
*while* the clarification is pending starts a new workflow instead of being
mistaken for the answer (Q29).

## Why orchestration before the extractor

Sprint 2's closing paragraph named Sprint 3 as the LLM boundary. The index in
`doc/implementation-plan.md` names it as orchestration. **The index wins**, and
the reason is worth stating rather than settling by seniority of file:

- Everything in this sprint is deterministic machinery — a compiled graph, a
  DAG walk, a checkpointer, a resume path, a fact guard. Sprint 2's whole
  argument was that such machinery must be falsifiable before a model's output
  becomes a variable inside it. Nothing about that argument changes one layer up.
- **The consumer already exists.** `#log supino 80kg` produces a real
  clarification today, built deterministically in Sprint 2, and it is currently
  discarded. Interrupts have something true to carry from day one — no prompt
  needed to manufacture a reason to pause.
- Sprint 4 then becomes **substitution, not construction**: `IntentRouterPort`
  gets an LLM implementation, `ExecutionPlannerPort` gets one, the strict-syntax
  adapter is deleted in favour of `WorkoutExtractor`, and the deterministic
  normalizer is replaced by `ResponseNormalizerAgent` behind the guard that this
  sprint already enforces. Every one of those is a node swap inside a graph
  whose edges are already tested.

The sprint file for Sprint 2 has been corrected in the same PR as this one, so
the two documents no longer disagree.

## Scope

### In

| Area | What ships | Traceability |
| --- | --- | --- |
| Graph runtime | `langgraph` pinned; a compiled static `MainGraph`; `graph_version` recorded on every execution | §11.1, Q121, Q132, DEC-002 |
| Checkpointer | `AsyncPostgresSaver` on a dedicated `langgraph` schema, `thread_id = conversation_id` | §11.5, Q123, Q124 |
| Graph state | `MainGraphState` as declared in §11.4 — working state and references only | §11.4, Q27 |
| Execution plan | `ExecutionTask`, `DependencyPolicy`, `ExecutionPlan`; parallelism derived from dependencies, never stored | §11.2, Q122, Q127 |
| Scheduler | READY fan-out, task-result reducer, `SKIPPED` propagation, terminal detection | §11.1, Q122, Q127 |
| Ops tables | `execution_tasks`, `pending_clarifications`; `workflow_executions` gains `WAITING_FOR_USER` and `PARTIAL_SUCCESS` | §26, Q118, Q125, Q128, Q28 |
| Clarification | `ClarificationSpec` (§40.2) frozen by a golden fixture; interrupt before dependent side effects | §15, §39.2, §40.2, Q126 |
| Resume | `PendingWorkflowResolver` classifying ANSWER / NEW_INTENT / CANCELLATION; `Command(resume=…)` | §39.2, Q29 |
| Partial success | Valid activities commit while ambiguous ones stay pending; the workflow ends `PARTIAL_SUCCESS` | Q28, Q56, Q57 |
| Learned aliases | An answered exercise clarification writes a user-scoped alias, so the same word resolves next time | §16 stage 1, Q41 |
| Response boundary | `ResultCollector` → deterministic normalizer → `ResponseGuard`, with the §25 fallback template | §25, Q59, Q62, Q136, DEC-004 |
| Worker | The workflow worker runs the graph; ACK still follows the outbound commit | §11, Q130 |
| Operations | DLQ inspection and replay as a command, not a manual `rabbitmqadmin` | §9.4, §29, §41 Phase 2 item 5 |
| ADRs | ADR-005 (from §43), ADR-015, ADR-016, each with a §48 traceability block | §43, §48 |

### Explicitly out

LLM providers, prompts, model registry and Langfuse spans · `IntentRouter` and
`ExecutionPlanner` as *models* (the ports ship, the implementations are
deterministic) · `WorkoutExtractor` — the strict-syntax adapter survives one
more sprint · `ResponseNormalizerAgent` — replies stay deterministic templates,
but they now pass the guard · corrections, undo and `CorrectionSubgraph` (§17,
Sprint 5) · analytics, recommendations, programs, memory, RAG · real Meta
WhatsApp integration · `WorkoutLoggingSubgraph` as a *subgraph* — the LOG_WORKOUT
handler stays a service call behind the registry (§11.3 explicitly allows this,
and Q129 says subgraphs are for meaningful boundaries, not for symmetry).

**Deliberate deviation from §11.1:** `normalize_input` is a pass-through this
sprint. The batch's fragments are already normalized text by the time the graph
sees them; the node exists so Sprint 4's transcript/emoji/unit normalization has
a place to land, and it is asserted to be identity today.

**Deliberate deviation from §11.2:** for real traffic the planner emits
single-task plans, because a deterministic router cannot discover a second
intent in a `#log` line. The DAG engine — dependencies, policies, fan-out,
skipping — is therefore exercised by plan-level tests over synthetic plans, not
by end-to-end traffic. This is stated as a gap rather than hidden: Sprint 4's
`ExecutionPlanner` is what makes multi-task plans arrive from real messages,
and the engine will already be correct when it does.

## Work breakdown

Ordered by dependency. Each workstream is independently reviewable, **ships its
own tests**, and leaves the tree green. Per `CLAUDE.md`: `feat/` branches for
implementation, `doc/` for documentation-only, `hotfix/` for defects found after
a merge, one PR each, reviewed and CI-green before merging.

### WS-1 — LangGraph runtime and the checkpointer boundary
1. Pin `langgraph` and `langgraph-checkpoint-postgres`. Both arrive with
   `langchain-core` behind them; that weight is accepted knowingly, and no
   module outside the graph package may import from it.
2. Migration `0010`: `CREATE SCHEMA langgraph`. Q124 asks for checkpoint storage
   isolated from domain tables, and the checkpointer offers no schema
   parameter — measured, not assumed: version 3.1.2 contains no such argument
   and creates four unqualified tables (`checkpoints`, `checkpoint_blobs`,
   `checkpoint_writes`, `checkpoint_migrations`). Isolation therefore comes from
   the connection's `search_path`.
3. `setup()` runs under the **admin** identity from `make migrate`, not from the
   worker: the workflow-worker role gets DML on that schema and never DDL (Q145).
4. A `CheckpointerProvider` bound to the worker's lifecycle, so the psycopg pool
   opens once and closes on shutdown (§37.4).
5. Tests: the four tables exist in `langgraph` and **none** of them exists in
   `public`; a checkpoint written by one provider is readable by a second one
   built from scratch (the restart case); the workflow-worker role can write a
   checkpoint and cannot create a table.

### WS-2 — Workflow operational tables
1. Migration `0011`: `execution_tasks` (§11.2's contract as columns, including
   `depends_on` and its per-dependency policy) and `pending_clarifications`
   (§40.2's contract, plus `status`, `expires_at` and the answering message).
2. `WorkflowExecutionStatus` gains `WAITING_FOR_USER` and `PARTIAL_SUCCESS`
   (Q28). A CHECK pairing `finished_at` with terminality, so a SUCCEEDED row
   without a finish time cannot exist.
3. A partial unique index: **at most one WAITING clarification per
   conversation**. Two open questions in one conversation make "the answer"
   ambiguous, and the database is where that invariant belongs.
4. Grants for both tables in `grants.py`, and the four lists Sprint 1 left
   behind updated in the same PR.
5. Tests: a second WAITING row for the same conversation is refused by the
   index; `execution_tasks` survives its execution being read back; no service
   role can delete a `pending_clarifications` row (they are closed, not erased).

### WS-3 — ExecutionPlan as data
1. Pure domain, no I/O: `TaskStatus`, `DependencyPolicy`
   (`REQUIRE_SUCCESS | ALLOW_PARTIAL | OPTIONAL`), `ExecutionTask`,
   `ExecutionPlan`.
2. `ready()` returns the tasks whose dependencies are satisfied — parallelism is
   *derived*, and `can_run_parallel` is deliberately absent (§11.2).
3. `apply()` folds one task result into the plan: a `REQUIRE_SUCCESS` dependency
   that failed marks its dependants `SKIPPED` transitively, `ALLOW_PARTIAL`
   lets them run with what exists, `OPTIONAL` never blocks.
4. A cycle is refused at construction, not discovered at scheduling time.
5. Tests: a diamond DAG schedules in two waves; a failed required predecessor
   skips a whole subtree and does not mark it FAILED; `ALLOW_PARTIAL` runs;
   `OPTIONAL` runs; a cycle raises; a plan is terminal only when no task is
   PENDING, READY or RUNNING.

### WS-4 — MainGraph, the static topology
1. `MainGraphState` exactly as §11.4 lists it. Q27 is a constraint on this type:
   references and working state, never a copy of the user's history.
2. The compiled graph with the §11.1 nodes. This sprint's bodies:
   `load_base_context` (identity, session, pending state), `resolve_pending_workflow`
   (WS-9), `normalize_input` (identity), `intent_router` (Sprint 2's `route()`
   behind `IntentRouterPort`), `execution_planner` (deterministic, behind
   `ExecutionPlannerPort`), then WS-5's scheduler and WS-6's response nodes.
3. `GRAPH_VERSION`, recorded on `workflow_executions` and on every log line
   (Q132). A graph change without a version bump is a test failure.
4. Compilation happens **once at process start**, not per message (Q121). A test
   asserts the compiled object is reused.
5. Tests: the compiled topology's nodes and edges equal the §11.1 list — a
   structural assertion, so an edge added by hand fails a test rather than
   quietly changing routing; the graph runs end to end against fakes with no
   database.

### WS-5 — Task scheduler, fan-out and the result reducer
1. `task_scheduler` sends every READY task through the handler registry and
   reduces results back into the plan until it is terminal (Q122).
2. Each transition is mirrored into `execution_tasks` (Q128) — outside graph
   state, because the point is to be able to ask "what is this workflow doing"
   with SQL while it runs.
3. A handler that raises marks its task FAILED with the error recorded, and the
   scheduler keeps going for independent tasks (Q28); it does not abort the plan.
4. Tests: two independent tasks both run from one scheduling pass; a failing
   task's dependant is SKIPPED and its independent sibling still COMPLETED; the
   `execution_tasks` rows tell the same story as the final plan; the loop is
   bounded and cannot spin on an unschedulable plan.

### WS-6 — Result collection, the response boundary and the guard
1. `collect_results` merges USER_VISIBLE results in plan order into a single
   response group; INTERNAL results contribute facts and no prose (§25).
2. `response_normalizer` is the deterministic pass-through this sprint — the
   templates Sprint 2 already writes — behind a `ResponseNormalizerPort`.
3. `response_guard` compares every verifiable fact in the `DomainResult`
   (exercise names, loads, repetitions, RPE, counts) against the text about to
   be sent. A missing or altered fact fails the guard, and the §25 deterministic
   fallback is what the user receives (Q62, Q136, DEC-004).
4. The workflow ends `PARTIAL_SUCCESS` when at least one task committed and at
   least one failed or is skipped — no global rollback of unrelated work (Q28).
5. Tests: a normalizer that drops the load fails the guard and the user gets the
   fallback rather than a wrong number; a normalizer that reorders words but
   keeps every fact passes; the guard is proven to fail by a deliberately lying
   fake, not only asserted to pass; ordering of a two-message group is preserved.

### WS-7 — The worker runs the graph
1. `WorkflowWorker.handle` invokes the compiled graph on
   `thread_id = conversation_id` instead of resolving a handler itself.
2. Everything Sprint 1 established is preserved and re-asserted, not assumed:
   one transaction for `workflow_executions` + `outbound_messages` +
   `domain_events` + `outbox_events`, ACK after that commit and never after
   provider delivery (Q130), redelivery of a completed batch producing nothing.
3. **The seam that matters:** the checkpointer writes on its own connection, in
   its own transaction. A domain transaction that rolls back after a checkpoint
   was written leaves a checkpoint describing work that does not exist. The
   answer is the one DEC-005 already gives — business effects are idempotent and
   `processed_operations` is authoritative, never the checkpoint — and this
   workstream proves it with failure injection rather than asserting it.
4. Tests: the existing worker, integration and E2E suites pass **unchanged** in
   observable behaviour; a batch redelivered after a checkpoint write but before
   the domain commit produces exactly one set; a workflow whose graph raises
   marks the execution FAILED and lets the broker retry.

### WS-8 — Clarification interrupts
1. `ClarificationSpec` as §40.2 declares it, frozen by a golden fixture in
   `tests/contract/` — Sprint 4's LLM-assisted clarification gets a contract
   rather than an example, the same way `StructuredWorkoutInput` did.
2. The LOG_WORKOUT handler stops answering-and-discarding. Deferred items with
   `MISSING_ESSENTIAL_DATA` or `AMBIGUOUS_EXERCISE` become a
   `ClarificationSpec`, the task goes `WAITING_FOR_USER`, the graph interrupts.
3. **Q126, concretely:** the valid activities of a mixed batch commit *before*
   the interrupt (Q56 requires it), and the interrupt happens before anything
   the pending item would write. The committed part carries the operation id it
   already had; the resumed part gets its own, derived from `clarification_id`,
   so resuming cannot rewrite what is already there.
4. **The pause is completed by the worker, not by the suspended graph.**
   `interrupt()` stops everything downstream of it, so the nodes that would
   normally write the reply never run. The worker reads the pending interrupt
   out of the returned state and writes the `pending_clarifications` row, the
   outbound question and the `WAITING_FOR_USER` status in the one transaction it
   already owns (Q125, Q130). A row without a question, or a question without a
   row, are both worse than neither — so they commit together.
5. Tests: `#log supino 80kg` writes zero sets, one WAITING row and one outbound
   question; a mixed batch writes the valid exercise and still asks about the
   other; the interrupt is proven to happen before the deferred item's write by
   asserting the row count, not by reading the code.

### WS-9 — Resume, and what an incoming message means
1. `PendingWorkflowResolver` (§39.2) classifies an incoming batch against the
   conversation's WAITING clarification: **ANSWER** when it parses against the
   spec's `expected_response_schema`, **NEW_INTENT** when it carries the strict
   marker or fails that parse, **CANCELLATION** on an explicit pt-BR cancel
   phrase. Deterministic this sprint; Q29's harder cases are Sprint 4's.
2. It runs in the **worker**, before the graph is invoked. A resumed run
   re-enters at the interrupted node and never visits the earlier nodes, so a
   classification computed inside the graph would run only on the path where its
   answer is not needed. §11.1's `resolve_pending_workflow` stays a node and
   records the decision; it does not compute it. The deviation is recorded in
   ADR-016.
3. An ANSWER resumes the original thread with `Command(resume=…)`, the deferred
   activity completes, and the confirmation names what was written.
4. A NEW_INTENT starts its own workflow and **leaves the pending clarification
   open** — Q29 says a pending question must not block unrelated work.
5. An answered *exercise* clarification writes a **user-scoped alias** into
   `exercise_aliases` — the resolver's stage 1 exists to be taught, the grant
   for it was already written in Sprint 2, and a user who explains a word once
   should not be asked about it again.
6. Clarifications expire: `expires_at`, default `PT6H`, swept by the existing
   `session-expiration-worker`. An expired question is closed, never answered
   later by an unrelated message.
7. Tests: the two-message round trip persists the sets on the second message and
   not the first; an answer arriving after expiry starts a new workflow instead
   of resuming a dead one; a `#log` during a pending clarification leaves the
   WAITING row untouched; a redelivered answer resumes once and writes one set;
   the cancel phrase closes the row and confirms the cancellation.

### WS-10 — Dead-letter inspection and replay
1. Sprint 1 built the retry tiers and the DLQs; nothing can currently get a
   message *out* of one. A `scripts/dlq.py` with `inspect` and `replay`
   subcommands, plus `make dlq-inspect` / `make dlq-replay`.
2. Replay republishes to the origin queue with the original routing key and an
   incremented replay count in the headers, so a message that dead-letters twice
   is visible as such rather than looking new.
3. Tests: a message dead-lettered by exhausting its tiers is listed by
   `inspect`; `replay` returns it to its origin queue and the worker processes it
   idempotently; replaying twice does not duplicate business effects.

### WS-11 — Cross-cutting verification
1. E2E: the full clarification round trip over the real stack — webhook,
   debounce, partitioned queue, graph, interrupt, checkpoint, resume, outbound.
2. Failure injection: a crash between the checkpoint write and the domain commit;
   a crash after the domain commit and before the ACK; both must converge on one
   set and one reply.
3. `make demo` gains the two-message exchange and fails if the second message
   does not persist the sets — the same standard as Sprint 2's demo.
4. A test asserting no module under `app/domain/` or `app/application/` imports
   `langgraph`. The graph is an adapter; the domain must not learn about it.

### WS-12 — Decision records
1. **ADR-005** — Static MainGraph with ExecutionPlan DAG as data. It is on §43's
   list, it has been unwritten since Sprint 1, and this is the sprint that earns
   it.
2. **ADR-015** — LangGraph checkpoint storage: dedicated schema, `search_path`
   isolation, admin-owned DDL, `thread_id = conversation_id`, and the explicit
   statement that a checkpoint is never authoritative for business state.
3. **ADR-016** — Clarification as an interrupt: why `pending_clarifications`
   exists beside the checkpoint (Q125), why at most one is open per
   conversation, and why an unanswered question expires.
4. `EXPECTED_ADRS` grows to ten; each record carries its §48 traceability block.

## Definition of Done

Every item mechanically verifiable — the sprint is closed by naming the test
that checks each line, not by agreement that it feels done.

- [ ] `make demo` completes the two-message clarification round trip against the running stack, and fails if the second message does not persist the sets.
- [ ] `make check` passes: unit, domain, contract, integration and E2E, with containers, in CI.
- [ ] No workstream merged without the tests listed under it, on a correctly prefixed branch with its own reviewed PR.
- [ ] `#log supino 80kg` writes zero `exercise_sets`, one WAITING `pending_clarifications` row, and one outbound question naming *repetições*.
- [ ] Answering `8 8 8` resumes that workflow and writes three sets attributed to both messages through `entity_sources`.
- [ ] A `#log` for a different exercise sent while a clarification is pending starts its own workflow and leaves the WAITING row untouched (Q29).
- [ ] The clarification question and its `pending_clarifications` row commit together, or neither exists.
- [ ] Every `workflow_executions` row records the `graph_version` that produced it, in SQL rather than only in a checkpoint (Q132).
- [ ] A terminal execution cannot have a NULL `finished_at`, and a `WAITING_FOR_USER` one must.
- [ ] Answering *which* exercise was meant writes a user alias, and the same raw name resolves without asking the second time (§16 stage 1).
- [ ] A clarification past `expires_at` is closed by the sweeper, and a later answer starts a new workflow rather than resuming a dead one.
- [ ] Two WAITING clarifications in one conversation are refused by a partial unique index, not by application code.
- [ ] The compiled graph's nodes and edges equal the §11.1 list, asserted structurally.
- [ ] The graph is compiled once per process, not per message (Q121).
- [ ] Checkpoint tables exist only in the `langgraph` schema; none of the four exists in `public` (Q124).
- [ ] The workflow-worker role can read and write checkpoints and cannot create a table in that schema (Q145).
- [ ] A checkpoint written before a rolled-back domain transaction produces exactly one set on redelivery, not zero and not two (DEC-005).
- [ ] `thread_id` equals `conversation_id` for every execution (Q123).
- [ ] A failed `REQUIRE_SUCCESS` predecessor leaves its dependants `SKIPPED`, and an independent sibling still `COMPLETED` (Q127, Q28).
- [ ] `execution_tasks` reports every task's terminal status, queryable without reading a checkpoint (Q118, Q128).
- [ ] A workflow with one committed task and one failed task ends `PARTIAL_SUCCESS` and does not roll back the committed work (Q28).
- [ ] A normalizer that alters a load, a repetition count or an exercise name fails the ResponseGuard, and the user receives the deterministic fallback (Q62, Q136).
- [ ] `ClarificationSpec` is frozen by a golden fixture: round-trip byte-identical, unknown fields refused.
- [ ] The ACK still follows the outbound commit, and a redelivered batch produces no second reply (Q130, DEC-005).
- [ ] A dead-lettered message can be listed and replayed by command, and replaying it twice produces one business effect.
- [ ] No module under `app/domain/` or `app/application/` imports `langgraph`; `mypy --strict` and `ruff` clean.
- [ ] ADR-005, ADR-015 and ADR-016 committed, each with a §48 traceability block; `EXPECTED_ADRS` is ten.

## Decisions needed

Resolve at the start; record as ADRs where structural. Per the lesson Sprint 2
paid for, every technical choice named here is a **starting point to be
measured**, not a commitment.

| # | Decision | Default if unspecified |
| --- | --- | --- |
| D1 | Orchestration library | **LangGraph 1.x**, pinned `>=1.2,<2`. The spec mandates it by name (§11, DEC-002); the cost is `langchain-core` in the dependency tree for a sprint with no LLM |
| D2 | Checkpoint isolation | **A dedicated `langgraph` schema**, selected by `search_path` on the checkpointer's connection. Measured: version 3.1.2 exposes no schema parameter. A separate database is the fallback if `search_path` proves unreliable |
| D3 | Who creates the checkpoint tables | **The admin identity, from `make migrate`.** The worker role gets DML only; a service role with DDL is a least-privilege hole (Q145) |
| D4 | `thread_id` | **`conversation_id`**, per Q123. Not `training_session_id`: sessions expire on their own clock and a clarification must survive that |
| D5 | Intent routing this sprint | **Sprint 2's `route()` behind `IntentRouterPort`.** The port is the Sprint 4 seam; nothing else changes |
| D6 | Planner this sprint | **Deterministic, single-task plans for real traffic.** The DAG engine is verified by plan-level tests over synthetic plans, and that gap is stated in Scope rather than papered over |
| D7 | Clarification transport | **LangGraph `interrupt()` + `Command(resume=…)`**, with `pending_clarifications` as the searchable mirror the checkpoint cannot be (Q125) |
| D8 | Classifying an incoming message | **Deterministic:** it is an ANSWER only if it parses against `expected_response_schema`; the marker or a parse failure means a new intent; an explicit pt-BR phrase cancels |
| D9 | Normalizer this sprint | **Deterministic pass-through behind `ResponseNormalizerPort`.** The guard, however, is real and enforced now — building it against a normalizer that cannot lie makes the test that proves it work harder than the code |
| D10 | Clarification lifetime | **`PT6H`**, configurable (§44). Long enough to survive a gym session, short enough that yesterday's question cannot capture today's message |
| D11 | Multiple open clarifications | **One per conversation**, enforced by a partial unique index. Several would make "the answer" ambiguous with no deterministic way to disambiguate |

## Risks

- **A checkpoint that outlives its transaction.** The checkpointer commits on
  its own connection; the domain commits on ours. Between them is a window
  where a checkpoint describes work that was rolled back. Mitigation: the
  checkpoint is never authoritative — `processed_operations` is — and WS-7
  injects a failure into exactly that window rather than reasoning about it.
- **The resume path is the new silent-corruption surface.** Sprint 2's worst
  outcome was a wrong exercise written silently; this sprint's is a resumed
  workflow writing a second copy of what it already wrote, or answering the
  wrong pending question. Mitigation: the resumed command derives its operation
  id from `clarification_id`, one WAITING row per conversation is a database
  invariant, and the round trip is asserted on row counts.
- **`langchain-core` arrives with LangGraph.** A large dependency tree enters a
  sprint that makes no model call. Mitigation: nothing outside `app/graphs/`
  may import it, asserted by a test; the ports that Sprint 4 fills are defined
  in the application layer with no LangChain types in their signatures.
- **The graph becoming a place to put logic.** A node is a coordination step,
  not a home for domain rules; the moment a node computes something a service
  should own, Sprint 2's boundaries start dissolving. Mitigation: the domain
  purity test, and handlers that stay service calls.
- **Scope creep toward corrections.** Interrupts and resume make correction look
  adjacent, and it is not: it needs entity reference resolution, which needs the
  LLM boundary. It stays in Sprint 5.
- **`search_path` isolation is a measured assumption, not a proven one.**
  Mitigation: WS-1's first test asserts where the four tables actually landed,
  and D2 names the fallback in advance so discovering it is a decision already
  taken rather than a mid-sprint scramble.

## Hand-off to Sprint 4

Sprint 3 ends with a system that can pause, remember why, and continue — and
that still cannot understand a sentence. Every remaining node is deterministic
and every seam an LLM will occupy is a typed port with a passing fake behind it:
`IntentRouterPort`, `ExecutionPlannerPort`, `ResponseNormalizerPort`, and the
strict-syntax adapter that ADR-013 has already sentenced. Sprint 4 fills them,
deletes the adapter, and turns `#log supino 80kg 10 9 8` into "fiz supino 80kg,
10 9 8, foi pesado" — against orchestration whose failure modes have already
been falsified, and behind a ResponseGuard that was written before there was
anything capable of lying to it.
