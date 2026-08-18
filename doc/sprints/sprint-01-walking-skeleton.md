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
| Tests | Each workstream ships its own; cross-cutting E2E, failure injection and correlation in WS-11 (§38) |
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

Ordered by dependency. Each workstream is independently reviewable, **ships its own tests**, and
leaves the tree green. Per `CLAUDE.md`: no implementation lands without tests covering it.
Only genuinely cross-cutting verification is deferred to WS-11.

### WS-1 — Project foundation
1. `pyproject.toml` with `src/app` layout; pin Python 3.13 to match the Nix devshell.
2. Create the §6 package tree with `__init__.py` only — no speculative modules.
3. Tooling: `ruff` (lint+format), `mypy` strict on `src/app`, `pytest` + `pytest-asyncio`.
4. `Makefile` or `justfile`: `fmt`, `lint`, `typecheck`, `test`, `up`, `migrate`.
5. CI workflow running lint → typecheck → tests on PR (§32.3 PR tier, minus evals).

**Tests:** a smoke test asserting the package imports and the test runner is wired; CI proven by
a deliberately failing commit on a scratch branch, so we know the gate actually blocks.

### WS-2 — Configuration
6. `ApplicationSettings` via `pydantic-settings`, nested per §34, **failing loudly at startup**.
7. `SecretsProvider` port + env-backed implementation.
8. `.env.example` committed; real `.env` gitignored.

**Tests:** valid env produces a fully populated settings object; missing required key raises at
startup with a legible message; an out-of-range value (e.g. `partitions = 0`) is rejected;
`repr()`/serialization of settings never exposes a secret value.

### WS-3 — Persistence base
9. SQLAlchemy async engine/session factory; `unit_of_work` context manager owning the transaction.
10. Base declarative model: UUIDv7 PK, `created_at`/`updated_at`, soft-delete mixin.
11. Repository **ports** in `application/ports`, implementations in `infrastructure/postgres` — domain must not import ORM types (§36).
12. Alembic wired to the settings object; migration 0001 for the tables listed above.

**Tests:** `unit_of_work` commits on success and rolls back on exception (integration, real
Postgres); soft-delete excludes rows from default queries; UUIDv7 values are monotonic within a
batch; migration 0001 applies and downgrades cleanly; an **architecture test** asserting no
module under `domain/` imports `sqlalchemy` — that boundary is easy to erode silently.

### WS-4 — Observability skeleton
13. Contextvar-based correlation context; middleware per request, helper per consumed message.
14. Structured JSON logging emitting `trace_id`, `correlation_id`, `workflow_execution_id`.
15. `TelemetryRedactor` with a deny-list (§30.3, §33.2).
16. `MetricsPort` and `AITracingPort` with no-op implementations.

**Tests:** correlation context survives an `await` boundary and does not leak between concurrent
tasks; a log record emitted inside a request carries the request's `trace_id`; the redactor
strips BSUID, phone numbers and secrets from a representative payload — table-driven, since this
is the test that protects §33 and it must be cheap to extend.

### WS-5 — Events and outbox
17. `DomainEventEnvelope` per §27.1.
18. `record_domain_event()` writing `domain_events` + `outbox_events(PENDING)` **inside the caller's transaction**.
19. `outbox-publisher`: `SELECT ... FOR UPDATE SKIP LOCKED`, publish, await confirm, mark `PUBLISHED`; batched, with backoff.

**Tests:** envelope serialization round-trip + a golden fixture freezing the wire format
(contract test, §38); a rolled-back transaction leaves **no** outbox row — the core atomicity
claim of §27; two concurrent publishers never claim the same row (`SKIP LOCKED` under real
Postgres); a publish failure leaves the row `PENDING` and it is retried; duplicate publication is
tolerated by the consumer via `event_id`.

### WS-6 — RabbitMQ topology
20. Declarative topology: exchanges `whatsapp.inbound`, `workflow`, `domain.events`, `background`, `whatsapp.outbound` (§9.1).
21. `workflow.00`–`workflow.31` with Single Active Consumer; one process may own several partitions.
22. **Stable partition hash** — `blake2b(user_id.bytes) % 32`, never Python `hash()` (§9.2).
23. Retry tiers 5s/30s/5m via delayed queues, then DLQ. No `sleep()` inside a consumer (§9.3, §9.4).

**Tests:** a **golden test with hardcoded user-ID → partition pairs**, freezing the hash contract
forever — this is the single most important unit test in the sprint, because changing it silently
reorders every user's messages; the hash is stable across processes (subprocess run); topology
declaration is idempotent on re-run; a message failing three times traverses 5s → 30s → 5m → DLQ
with the original idempotency key intact; SAC serializes two concurrent messages for one user.

### WS-7 — Inbound ingress
24. `POST /webhooks/whatsapp`: signature verification, parse, identity resolution, dedupe on `UNIQUE(provider, external_message_id)`, persist `messages`, publish `message.received`, return fast (§8, §35.1).
25. Conversation resolution: reuse active conversation or rotate on inactivity (§7.2).
26. `GET /health` (liveness) and `GET /ready` (per-process dependency checks).
27. BSUID as ciphertext + HMAC lookup, `UNIQUE(provider, external_id_lookup_hmac)` (§7.1).

**Tests:** an invalid signature is rejected before any persistence; the same
`external_message_id` delivered twice yields exactly one `messages` row; first contact creates
`users` + `user_identifiers`, second contact reuses them; conversation rotates only past the
inactivity threshold; HMAC lookup finds a user without decrypting; `/ready` fails when Postgres
is down and `/health` still succeeds; a **contract test** for the WhatsApp payload parser against
recorded fixtures (§38), so a provider format change fails loudly.

### WS-8 — Debounce and batching
28. `message-aggregator` consuming `message.received`.
29. Redis debounce keyed `debounce:v1:user:{id}:conversation:{id}`, 3s sliding, 10s absolute cap, TTL always set (§8, §10).
30. **Generation counter** so a stale timer cannot flush a batch that has since grown. *Design note:* the flush trigger is a delayed RabbitMQ message carrying the generation it was scheduled for; on consume, a generation mismatch means drop. Survives restarts — an implementation choice the spec leaves open.
31. Persist `message_batches` + ordered `message_batch_items`, emit `InputBatchReady` to the user's partition.

**Tests:** the generation logic is unit-tested **in isolation from Redis first** (pure function
over state transitions), because this is the subtlest correctness surface in the sprint; then
integration: three messages within the window produce one batch; a message at 9s into the 10s cap
still respects the absolute window; a stale flush is dropped without emitting; batch items
preserve arrival order; every debounce key has a TTL (no unbounded Redis growth); **Redis flushed
mid-window loses no message**, since §10 declares Redis non-authoritative and recovery from
persisted `messages` must hold.

### WS-9 — Workflow worker (stub)
32. Consume `InputBatchReady`; create or resume `workflow_executions` keyed by batch (§28).
33. **Stub handler** producing a fixed `DomainResult`. Keep the handler-registry shape from §11.3 so Sprint 3's `MainGraph` is an additive replacement.
34. One transaction: persist `outbound_messages` (`response_group_id` + `sequence`), append `domain_events`, insert `outbox_events`. ACK only after commit (§9.3, §25).

**Tests:** redelivery of the same batch resumes the existing `workflow_execution` and creates no
second one; the domain rows, outbound rows and outbox row commit atomically or not at all; ACK
happens strictly after commit (asserted by ordering, not by inspection); the handler registry
resolves a task type to its handler and raises on an unknown type.

### WS-10 — Outbound dispatch
35. `whatsapp-dispatcher` sending sequence N only after N−1 is dispatch-safe (§25).
36. `WhatsAppClient` port + `FakeWhatsAppClient`.
37. Delivery-state transitions persisted on `outbound_messages`.

**Tests:** a three-message response group is delivered in sequence order, never interleaved;
a send failure on sequence 2 does not dispatch sequence 3; a retried send does not duplicate a
delivered message; delivery-state transitions are persisted and monotonic.

### WS-11 — Cross-cutting verification
Only what cannot belong to a single workstream.

38. **E2E:** three fragmented messages → one batch → one workflow → one ordered reply in the fake client, asserted from the outside.
39. **Failure injection (§38):** kill the worker after commit but before ACK; on redelivery assert exactly one `outbound_message` and one workflow execution. Also: duplicate outbox publish, Redis loss mid-window, broker disconnect during publish.
40. **Correlation:** the `trace_id` minted at the webhook appears in the dispatcher's log line for the same interaction, across three process boundaries.
41. Test infrastructure: Testcontainers fixtures for Postgres/RabbitMQ/Redis, shared and session-scoped so the suite stays fast enough to run on every PR.

### WS-12 — Decision records
42. ADR-001 modular monolith with worker entrypoints; ADR-003 at-least-once + outbox + idempotency; ADR-004 32 partitions + SAC + the frozen hash.
43. `doc/adr/` with a template. Remaining ADRs from §43 are written when their decision is taken.

## Definition of Done

Every item is mechanically verifiable — no "works on my machine".

- [ ] `docker compose up` yields a system that accepts a webhook and produces a reply, from a clean clone.
- [ ] `make test` passes: unit + integration + E2E, with containers, in CI.
- [ ] No workstream was merged without the tests listed under it (`CLAUDE.md` rule 2).
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
