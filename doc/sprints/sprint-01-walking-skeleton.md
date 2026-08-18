# Sprint 1 — Walking Skeleton

**Status:** Planned
**Spec basis:** §5, §6, §7, §8, §9, §10, §26, §27, §28, §34, §35, §36, §37, §38, §41 Phase 0
**Depends on:** nothing (repo currently contains only the spec and a Nix devshell)

## Goal

A real WhatsApp-shaped inbound message travels the **entire architectural spine** and comes back
as a deterministic reply:

```text
webhook -> persist message -> RabbitMQ -> debounce -> MessageBatch
        -> workflow partition -> worker -> DomainResult
        -> outbound_message + outbox -> dispatcher -> reply
```

**Zero intelligence in this sprint.** No LLM, no LangGraph, no exercise catalog, no workout
domain. The worker's entire logic is: take the batch text, produce a fixed acknowledgement.
The point is not what it says — it is that every durability and idempotency invariant in §27 and
§28 is real, tested, and load-bearing before any probabilistic component is allowed near it.

**Demo at end of sprint:** send three fragmented messages within two seconds, observe them
batched into one `message_batch`, one workflow execution, one reply; then replay the same
RabbitMQ delivery and observe *no* duplicate side effects.

## Why a skeleton instead of §41 Phase 0 verbatim

Phase 0 as written produces no traffic, so its hardest guarantees — at-least-once redelivery,
outbox publication, idempotent business effects, correlation across process boundaries — stay
theoretical. Threading a trivial vertical slice through them costs little extra and converts all
of them into something a test can fail on. The Phase 0 checklist is fully retained below; it is
only *reordered* around a working path.

## Scope

### In

| Area | What ships |
| --- | --- |
| Packaging | `pyproject.toml`, `src/app/` skeleton per §6, dependency + lint + typecheck toolchain |
| Config | Typed `ApplicationSettings` validating at startup (§34), secrets separated from config |
| Local infra | `docker compose`: postgres, rabbitmq, redis (§37.2) |
| Persistence | SQLAlchemy + Alembic base, UUID/timestamp mixins, transaction helper, repository ports (§36) |
| Schema | Migration 0001: `users`, `user_identifiers`, `conversations`, `messages`, `message_batches`, `message_batch_items`, `workflow_executions`, `processed_operations`, `outbound_messages`, `domain_events`, `outbox_events` |
| Correlation | `trace_id` / `correlation_id` / `workflow_execution_id` contextvars, structured logs carrying them (§30.3) |
| Events | `DomainEventEnvelope` (§27.1), transactional outbox write, `OutboxPublisher` with `FOR UPDATE SKIP LOCKED` + publisher confirms (§27) |
| Ingress | `POST /webhooks/whatsapp`, `GET /health`, `GET /ready` (§35) |
| Messaging | Exchange topology, 32 workflow partitions, Single Active Consumer, stable partition hash, retry tiers + DLQ (§9) |
| Aggregation | Redis debounce, 3s sliding / 10s absolute, generation counter (§8, §10) |
| Workflow | `workflow-worker` with a **stub handler** producing a fixed `DomainResult` |
| Outbound | `outbound_messages` with `response_group_id` + `sequence`, dispatcher honouring sequence (§25) |
| Adapters | `FakeWhatsAppClient` and fake STT — no real Meta call in this sprint |
| Tests | Unit, integration on ephemeral containers, one E2E, redelivery failure-injection (§38) |
| ADRs | ADR-001, ADR-003, ADR-004 — the decisions this skeleton actually commits to |

### Explicitly out

LLM providers and prompts · LangGraph and checkpointing · `ExecutionPlanner` and the task DAG ·
exercise catalog and resolver · workout/session/set domain · corrections and undo · analytics ·
RAG and Qdrant · programs · memory · real Meta WhatsApp integration · Datadog and Langfuse
backends (ports defined, no-op implementations only) · Kubernetes · authentication beyond
webhook signature verification.

Qdrant is **not** in the compose file this sprint. Nothing retrieves yet, and an unused service
is a maintenance cost with no test to justify it.

## Work breakdown

Ordered by dependency. Each item is independently reviewable and leaves the tree green.

### WS-1 — Project foundation
1. `pyproject.toml` with `src/app` layout; pin Python 3.13 to match the Nix devshell.
2. Create the §6 package tree with `__init__.py` only — no speculative modules.
3. Tooling: `ruff` (lint+format), `mypy` strict on `src/app`, `pytest` + `pytest-asyncio`.
4. `Makefile` or `justfile`: `fmt`, `lint`, `typecheck`, `test`, `up`, `migrate`.
5. CI workflow running lint → typecheck → unit tests on PR (§32.3 PR tier, minus evals).

### WS-2 — Configuration
6. `ApplicationSettings` via `pydantic-settings`, nested per §34, **failing loudly at startup**.
7. `SecretsProvider` port + env-backed implementation; assert no secret is logged.
8. `.env.example` committed; real `.env` gitignored.

### WS-3 — Persistence base
9. SQLAlchemy async engine/session factory; `unit_of_work` context manager owning the transaction.
10. Base declarative model: UUIDv7-or-v4 PK, `created_at`/`updated_at`, soft-delete mixin.
11. Repository **ports** in `application/ports`, concrete implementations in `infrastructure/postgres` — domain must not import ORM types (§36).
12. Alembic wired to the settings object; migration 0001 for the tables listed above.

### WS-4 — Observability skeleton
13. Contextvar-based correlation context; middleware populating it per request, worker helper per message.
14. Structured JSON logging emitting `trace_id`, `correlation_id`, `workflow_execution_id`.
15. `TelemetryRedactor` with a deny-list; unit-tested that a BSUID never reaches a log record (§30.3, §33.2).
16. `MetricsPort` and `AITracingPort` with no-op implementations — Datadog/Langfuse arrive later without touching call sites.

### WS-5 — Events and outbox
17. `DomainEventEnvelope` dataclass/model per §27.1 with a serialization contract test.
18. `record_domain_event()` writing `domain_events` + `outbox_events(PENDING)` **inside the caller's transaction**.
19. `outbox-publisher` worker: `SELECT ... FOR UPDATE SKIP LOCKED`, publish, await confirm, mark `PUBLISHED`; batched, with backoff.

### WS-6 — RabbitMQ topology
20. Declarative topology module: exchanges `whatsapp.inbound`, `workflow`, `domain.events`, `background`, `whatsapp.outbound` (§9.1).
21. `workflow.00`–`workflow.31` with Single Active Consumer; one worker process may own several partitions.
22. **Stable partition hash** — `blake2b(user_id.bytes) % 32`, never Python `hash()` (§9.2). Frozen by a golden test with hardcoded expected values.
23. Retry tiers 5s/30s/5m via delayed queues, then DLQ. No `sleep()` inside a consumer (§9.3, §9.4).

### WS-7 — Inbound ingress
24. `POST /webhooks/whatsapp`: signature verification, payload parse, identity resolution (create `users` + `user_identifiers` on first contact), dedupe on `UNIQUE(provider, external_message_id)`, persist `messages`, publish `message.received`, return fast (§8, §35.1).
25. Conversation resolution: reuse the active conversation or rotate on inactivity (§7.2).
26. `GET /health` (liveness, no dependencies) and `GET /ready` (checks the dependencies *this* process needs).
27. BSUID stored as ciphertext + HMAC lookup, `UNIQUE(provider, external_id_lookup_hmac)` (§7.1).

### WS-8 — Debounce and batching
28. `message-aggregator` consuming `message.received`.
29. Redis debounce keyed `debounce:v1:user:{id}:conversation:{id}`, 3s sliding, 10s absolute cap, TTL always set (§8, §10).
30. **Generation counter** so a stale timer cannot flush a batch that has since grown. *Design note:* the flush trigger is a delayed RabbitMQ message carrying the generation it was scheduled for; on consume, a generation mismatch means drop. This avoids in-process timers and survives restarts — an implementation choice the spec leaves open.
31. Persist `message_batches` + ordered `message_batch_items`, emit `InputBatchReady` to the user's workflow partition.

### WS-9 — Workflow worker (stub)
32. Consume `InputBatchReady`; create or resume `workflow_executions` keyed by batch — redelivery must resume, never duplicate (§28).
33. **Stub handler**: build a fixed `DomainResult` acknowledging receipt. This is the seam Sprint 3 replaces with `MainGraph`; keep the handler-registry shape from §11.3 so the replacement is additive.
34. In one transaction: persist `outbound_messages` (with `response_group_id` + `sequence`), append `domain_events`, insert `outbox_events`. ACK only after commit (§9.3, §25).

### WS-10 — Outbound dispatch
35. `whatsapp-dispatcher` consuming outbound events, sending sequence N only after N−1 is dispatch-safe (§25).
36. `WhatsAppClient` port + `FakeWhatsAppClient` writing to a log/file, asserted by the E2E test.
37. Delivery-state transitions persisted on `outbound_messages`.

### WS-11 — Tests
38. **Unit:** partition hash stability, debounce generation logic, retry policy, envelope serialization, redactor.
39. **Integration (Testcontainers):** dedupe under duplicate webhook, outbox publish-and-mark, batch assembly, transaction rollback leaves no orphan outbox row.
40. **E2E:** three fragmented messages → one batch → one reply in the fake client.
41. **Failure injection (§38):** kill the worker after commit but before ACK; on redelivery assert exactly one `outbound_message` and one workflow execution.

### WS-12 — Decision records
42. ADR-001 modular monolith with worker entrypoints; ADR-003 at-least-once + outbox + idempotency; ADR-004 32 partitions + SAC + the frozen hash.
43. `doc/adr/` with a template. Remaining ADRs from §43 are written when their decision is actually taken.

## Definition of Done

Every item is mechanically verifiable — no "works on my machine".

- [ ] `docker compose up` yields a system that accepts a webhook and produces a reply, from a clean clone.
- [ ] `make test` passes: unit + integration + E2E, with containers, in CI.
- [ ] Duplicate webhook delivery (same `external_message_id`) creates exactly one `messages` row.
- [ ] Redelivery of a workflow message after a post-commit crash creates **no** second `outbound_message`.
- [ ] Three messages inside the debounce window produce exactly one `message_batch` and one reply.
- [ ] A message arriving at 9s into a 10s cap still respects the absolute window.
- [ ] Partition assignment for a fixed set of user IDs matches the golden test — the hash contract is frozen.
- [ ] Every outbox row reaches `PUBLISHED`, or is visibly stuck and alertable; none are silently lost.
- [ ] `trace_id` and `correlation_id` from the webhook appear in the dispatcher's logs for the same interaction.
- [ ] No BSUID, phone number or secret appears in any log line (asserted, not assumed).
- [ ] Startup fails fast and legibly on missing or invalid configuration.
- [ ] `mypy --strict` clean on `src/app`; `ruff` clean.
- [ ] ADR-001, ADR-003, ADR-004 committed.

## Decisions needed

These block or shape work inside the sprint. Resolve at the start, record as ADRs where structural.

| # | Decision | Default if unspecified |
| --- | --- | --- |
| D1 | Dependency manager: `uv` / Poetry / pip-tools | **`uv`** — fastest, lockfile, composes well with the Nix venv already in the repo |
| D2 | Async or sync SQLAlchemy | **Async** — the whole system is I/O-bound workers; converting later is invasive |
| D3 | RabbitMQ client: `aio-pika` / `pika` | **`aio-pika`**, following D2 |
| D4 | Delayed-message mechanism: `rabbitmq_delayed_message_exchange` plugin vs per-tier TTL+DLX queues | **TTL + DLX queues** — no plugin dependency, works on any managed broker |
| D5 | UUIDv7 or UUIDv4 for PKs | **UUIDv7** — time-ordered, materially better index locality on append-heavy tables like `messages` |
| D6 | Is a real Meta WhatsApp number available for testing? | Assume **no**; fake client only, real integration deferred |
| D7 | Cloud target (affects compose parity, not architecture) | Defer — §37.1 keeps the doc vendor-neutral |

## Risks

- **Debounce correctness is the subtlest thing in this sprint.** Sliding window, absolute cap and
  generation counter interact, and the failure mode is silent message loss or split batches.
  Mitigation: unit-test the generation logic in isolation before wiring it to Redis.
- **Single Active Consumer plus 32 partitions is easy to misconfigure** in a way that only shows
  up under concurrent load from one user. Mitigation: an integration test driving two concurrent
  messages for the same user and asserting serialized processing.
- **The stub handler is a temptation.** It must stay trivial. Any domain logic that leaks into it
  will have to be moved in Sprint 2 — write it as an obviously-temporary seam.
- **Scope creep toward the workout domain.** The moment `exercise` appears in this sprint's diff,
  the sprint is off-plan.

## Hand-off to Sprint 2

Sprint 1 ends with a durable, observable, idempotent pipeline whose only weakness is that it has
nothing intelligent to say. Sprint 2 replaces the stub handler with the **deterministic** workout
domain — activity model, `ActivitySchemaRegistry`, `ActivityValidator`, exercise catalog and the
non-LLM stages of the resolver (§14, §16) — still with no LLM, driven by hand-written structured
input. That keeps domain correctness falsifiable before extraction quality becomes a variable.
