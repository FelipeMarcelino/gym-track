# Sprint 1 — Walking Skeleton

**Status:** Done — 2026-08-19
**Spec basis:** v1.1 — §5, §6, §7, §8, §9, §10, §26, §27, §28, §30, §33, §34, §35, §36, §37, §38, §41 Phase 0, §48
**Decision records exercised:** DEC-005, DEC-006, DEC-010, DEC-013, DEC-015
**Depends on:** nothing (repo contains only the spec, `CLAUDE.md` and a Nix devshell)

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
The point is not what it says — it is that every durability and idempotency invariant behind
DEC-005 is real, tested and load-bearing before any probabilistic component is allowed near it.

**Demo at end of sprint:** send three fragmented messages within two seconds, observe them
batched into one `message_batch`, one workflow execution, one reply; then replay the same
RabbitMQ delivery and observe *no* duplicate side effects.

## Why a skeleton instead of §41 Phase 0 verbatim

Phase 0 as written produces no traffic, so its hardest guarantees — at-least-once redelivery,
outbox publication, idempotent business effects, correlation across process boundaries — stay
theoretical. Threading a trivial vertical slice through them costs little extra and converts all
of them into something a test can fail on. The Phase 0 checklist is fully retained below; it is
only *reordered* around a working path.

## What changed from spec v1.0 to v1.1

Architecture sections §1–45 are **byte-identical** between v1.0 and v1.1. v1.1 adds the
traceability appendix (§46 Q1–Q160, §47 DEC-001..015, §48). No decision was reversed.

The appendix is nonetheless load-bearing for this sprint, because several rows state operational
requirements that the narrative sections left implicit. Newly explicit in this sprint's scope:

| Source | Requirement | Where it lands |
| --- | --- | --- |
| Q117 | DLQ needs inspect/replay/discard tooling, and replay MUST preserve original idempotency keys | WS-6 |
| Q131 | Exactly **one main trace per InputBatch**; background jobs get their own trace linked by `correlation_id` | WS-4 |
| Q145 | Least privilege **between infrastructure services** — service-specific DB roles and credentials | WS-2, WS-3 |
| Q120 | A failed later message MUST NOT resend already-delivered earlier messages of the group | WS-10 |
| Q130 | ACK after outbound/outbox persistence — explicitly **not** after provider delivery | WS-9 |
| Q115 | Initial workflow prefetch is 1, revisited only with a benchmark | WS-6 |
| §48 | Every ADR MUST link back to the affected spec section and, where applicable, the Q decision | WS-12 |

## Scope

### In

| Area | What ships | Traceability |
| --- | --- | --- |
| Packaging | `pyproject.toml`, `src/app/` skeleton, lint + typecheck toolchain | §6, DEC-015 |
| Config | Typed `ApplicationSettings` validating at startup; secrets via `SecretsProvider` | §34, Q144, Q154 |
| Local infra | `docker compose`: postgres, rabbitmq, redis | §37.2, Q157 |
| Persistence | SQLAlchemy + Alembic base, UUID/timestamp mixins, `unit_of_work`, repository ports | §36, Q155 |
| Schema | Migration 0001: `users`, `user_identifiers`, `conversations`, `messages`, `message_batches`, `message_batch_items`, `workflow_executions`, `processed_operations`, `outbound_messages`, `domain_events`, `outbox_events` | §26 |
| DB roles | Per-service Postgres roles with least-privilege grants | Q145 |
| Correlation | One trace per InputBatch; `trace_id`/`correlation_id`/`workflow_execution_id` contextvars | §30.3, Q131, DEC-013 |
| Redaction | `TelemetryRedactor` running before telemetry leaves the process | §30.3, Q146 |
| Events | `DomainEventEnvelope`, transactional outbox, `OutboxPublisher` with `FOR UPDATE SKIP LOCKED` | §27, Q25, DEC-005 |
| Ingress | `POST /webhooks/whatsapp`, `GET /health`, `GET /ready` | §35, Q112, Q153 |
| Identity | BSUID as ciphertext + HMAC lookup | §7.1, Q143 |
| Messaging | Exchange topology, 32 partitions, SAC, stable hash, prefetch 1, retry tiers, DLQ + replay tooling | §9, Q111, Q114-Q117, DEC-006 |
| Aggregation | Redis debounce, 3s sliding / 10s absolute, generation counter | §8, §10, Q11, Q113, DEC-010 |
| Workflow | `workflow-worker` with a **stub handler** producing a fixed `DomainResult` | §11.3, Q130 |
| Outbound | `outbound_messages` with `response_group_id` + `sequence`, sequence-respecting dispatcher | §25, Q119-Q120 |
| Adapters | `FakeWhatsAppClient` — no real Meta call this sprint | — |
| Tests | Each workstream ships its own; cross-cutting work in WS-11 | §38, Q158 |
| ADRs | ADR-001, ADR-003, ADR-004, ADR-011, each with §48 traceability links | §43, §48 |

### Explicitly out

LLM providers and prompts · LangGraph and checkpointing · `ExecutionPlanner`, `execution_tasks`
and the task DAG · exercise catalog and resolver · workout/session/set domain · corrections and
undo · analytics · RAG and Qdrant · programs · memory · real Meta WhatsApp integration · Datadog
and Langfuse backends (ports defined, no-op implementations only) · `FeatureFlagProvider` (Q156 —
its only consumers are AI rollouts that do not exist yet) · Kubernetes · authentication beyond
webhook signature verification.

**Deliberate deviation from Q157:** Qdrant is *not* in the compose file this sprint. Nothing
retrieves yet, and an unused service is a maintenance cost with no test to justify it. It joins
compose in Sprint 8, when the knowledge pipeline needs it. Recorded here so the deviation is
visible rather than silent.

## Work breakdown

Ordered by dependency. Each workstream is independently reviewable, **ships its own tests**, and
leaves the tree green. Per `CLAUDE.md`: no implementation lands without tests covering it, and
each workstream is its own branch and PR, prefixed by what it contains: `feat/` for the
implementation workstreams (WS-1..WS-11), `doc/` for documentation-only ones (WS-12 is ADRs and a
template — no code), `hotfix/` for defects found after a workstream merges.

### WS-1 — Project foundation
1. `pyproject.toml` with `src/app` layout; pin Python 3.13 to match the Nix devshell.
2. Create the §6 package tree with `__init__.py` only — no speculative modules.
3. Tooling: `ruff` (lint+format), `mypy` strict on `src/app`, `pytest` + `pytest-asyncio`.
4. `Makefile` or `justfile`: `fmt`, `lint`, `typecheck`, `test`, `up`, `migrate`.
5. CI workflow running lint → typecheck → tests on PR (§32.3 PR tier, minus evals).

**Tests:** smoke test asserting the package imports and the runner is wired; CI gate proven to
actually block by a deliberately failing commit on a scratch branch.

### WS-2 — Configuration and secrets
6. `ApplicationSettings` via `pydantic-settings`, nested per §34, **failing loudly at startup**.
7. `SecretsProvider` port + env-backed implementation (Q144).
8. Per-service credential sets in config — `api`, `message-aggregator`, `workflow-worker`, `outbox-publisher`, `dispatcher` each carry their own DB user (Q145). The set must cover **every process that opens a connection**; `message-aggregator` writes `message_batches` and `message_batch_items` in WS-8, so it needs its own role rather than borrowing one.
9. `.env.example` committed; real `.env` gitignored.

**Tests:** valid env produces a fully populated settings object; a missing required key raises at
startup with a legible message; an out-of-range value (`partitions = 0`, `debounce > max_batch`)
is rejected; `repr()` and serialization of settings never expose a secret value.

### WS-3 — Persistence base
10. SQLAlchemy async engine/session factory; `unit_of_work` context manager owning the transaction.
11. Base declarative model: UUIDv7 PK, `created_at`/`updated_at`, soft-delete mixin.
12. Repository **ports** in `application/ports`, implementations in `infrastructure/postgres` (Q155).
13. Alembic wired to settings; migration 0001 for the tables above.
14. Migration creating per-service roles and grants: the dispatcher cannot write `messages`, the API cannot write `outbound_messages` (Q145).

**Tests:** `unit_of_work` commits on success and rolls back on exception (real Postgres);
soft-delete excludes rows from default queries; UUIDv7 values are monotonic within a batch;
migration 0001 applies and downgrades cleanly; an **architecture test** asserting no module under
`domain/` imports `sqlalchemy`; **grant tests** asserting each service role is refused the writes
it should not have — least privilege is worthless if nothing verifies it.

### WS-4 — Observability skeleton
15. Contextvar correlation context; middleware per request, helper per consumed message.
16. **One main trace per InputBatch** (Q131). The interaction trace is minted by the **aggregator, when the `MessageBatch` is persisted** — not at the webhook. Fragments arrive as N independent webhook requests, and no request can know which batch it will join, so a webhook-minted trace could never be the single shared trace Q131 requires. Each webhook request keeps its own short request trace; `messages` stores that request `trace_id`, and the batch trace links to the request traces it absorbed. Background work started later opens a *new* trace linked by `correlation_id`.
17. Structured JSON logging emitting `trace_id`, `correlation_id`, `workflow_execution_id`.
18. `TelemetryRedactor` with a deny-list, running before egress (Q146).
19. `MetricsPort` and `AITracingPort` with no-op implementations (DEC-013 keeps the two domains separable from day one).

**Tests:** correlation context survives an `await` boundary and does not leak between concurrent
tasks; a log record emitted inside a request carries that request's `trace_id`; **three fragments
arriving as three separate webhook requests produce three request traces but exactly one
interaction trace**, which links back to all three; a background job gets a distinct trace
carrying the same `correlation_id`; the redactor strips BSUID, phone numbers and secrets — table-driven, since this
test protects §33 and must be cheap to extend.

### WS-5 — Events and outbox
20. `DomainEventEnvelope` per §27.1.
21. `record_domain_event()` writing `domain_events` + `outbox_events(PENDING)` **inside the caller's transaction**.
22. `outbox-publisher`: `SELECT ... FOR UPDATE SKIP LOCKED`, publish, await confirm, mark `PUBLISHED`; batched, with backoff.

**Tests:** envelope round-trip plus a **golden fixture freezing the wire format** (contract test,
§38); a rolled-back transaction leaves **no** outbox row — the atomicity claim of DEC-005; two
concurrent publishers never claim the same row under real Postgres; a publish failure leaves the
row `PENDING` for retry; duplicate publication is tolerated downstream via `event_id`.

### WS-6 — RabbitMQ topology
23. Declarative topology: `whatsapp.inbound`, `workflow`, `domain.events`, `background`, `whatsapp.outbound` (Q111).
24. `workflow.00`–`workflow.31`, Single Active Consumer, **prefetch 1** (Q115); one process may own several partitions.
25. **Stable partition hash** — `blake2b(user_id.bytes) % 32`, never Python `hash()` (§9.2, DEC-006).
26. Retry tiers 5s/30s/5m via TTL+DLX queues, then DLQ. No `sleep()` inside a consumer (Q116).
27. **DLQ tooling** (Q117): inspect, replay, discard. Replay MUST re-publish with the original idempotency keys intact.

**Tests:** a **golden test with hardcoded user-ID → partition pairs**, freezing the hash contract
— the single most important unit test in this sprint, because a silent change reorders every
user's message stream; hash stability verified across a subprocess boundary; topology declaration
is idempotent on re-run; a message failing three times traverses 5s → 30s → 5m → DLQ; **a replayed
DLQ message produces no duplicate business effect**, which is the whole point of Q117; SAC
serializes two concurrent messages for one user.

### WS-7 — Inbound ingress
28. `POST /webhooks/whatsapp`: signature verification, parse, identity resolution, dedupe on `UNIQUE(provider, external_message_id)`, persist `messages` **including the request `trace_id`** so the aggregator can link it to the interaction trace (WS-4 item 16), publish `message.received`, return fast (Q112, Q153).
29. Conversation resolution: reuse the active conversation or rotate on inactivity (§7.2, Q30).
30. `GET /health` (liveness) and `GET /ready` (per-process dependency checks).
31. BSUID as ciphertext + HMAC lookup, `UNIQUE(provider, external_id_lookup_hmac)` (Q143).

**Tests:** an invalid signature is rejected before any persistence; the same `external_message_id`
delivered twice yields exactly one `messages` row; first contact creates `users` +
`user_identifiers`, second reuses them; conversation rotates only past the inactivity threshold;
HMAC lookup finds a user without decrypting; `/ready` fails when Postgres is down while `/health`
still succeeds; a **contract test** for the WhatsApp payload parser against recorded fixtures, so
a provider format change fails loudly rather than silently dropping messages.

### WS-8 — Debounce and batching
32. `message-aggregator` consuming `message.received`.
33. Redis debounce keyed `debounce:v1:user:{id}:conversation:{id}`, 3s sliding, 10s absolute cap, TTL always set (Q11, DEC-010).
34. **Generation counter** so a stale timer cannot flush a batch that has since grown (Q113). *Implementation choice:* the flush trigger is a delayed RabbitMQ message carrying the generation it was scheduled for; on consume, a generation mismatch means drop. This avoids in-process timers and survives restarts. The spec mandates the generation guard, not the mechanism — recorded as ADR-011.
35. Persist `message_batches` + ordered `message_batch_items`, emit `InputBatchReady` to the user's partition.

**Tests:** the generation logic is unit-tested **as a pure function before Redis is involved** —
this is the subtlest correctness surface in the sprint; then integration: three messages within
the window produce one batch; a message at 9s into the 10s cap still respects the absolute
window; a stale flush is dropped without emitting; batch items preserve arrival order; every
debounce key has a TTL; **Redis flushed mid-window loses no message**, since §10 declares Redis
non-authoritative and recovery from persisted `messages` must hold.

### WS-9 — Workflow worker (stub)
36. Consume `InputBatchReady`; create or resume `workflow_executions` keyed by batch (§28, Q118).
37. **Stub handler** producing a fixed `DomainResult`. Keep the handler-registry shape from §11.3 so Sprint 3's `MainGraph` is an additive replacement, not a rewrite.
38. One transaction: persist `outbound_messages` (`response_group_id` + `sequence`), append `domain_events`, insert `outbox_events`. **ACK after that commit — explicitly not after provider delivery** (Q130).

**Tests:** redelivery of the same batch resumes the existing `workflow_execution` and creates no
second one; domain rows, outbound rows and the outbox row commit atomically or not at all; ACK
happens strictly after commit, asserted by ordering; the handler registry resolves a task type and
raises on an unknown one.

### WS-10 — Outbound dispatch
39. `whatsapp-dispatcher` sending sequence N only after N−1 is dispatch-safe (§25).
40. `WhatsAppClient` port + `FakeWhatsAppClient`; the dispatcher owns provider retries (Q119).
41. Delivery-state transitions persisted on `outbound_messages`.

**Tests:** a three-message response group is delivered in sequence order, never interleaved;
a send failure on sequence 2 does not dispatch sequence 3; **retrying a group after a mid-group
failure does not resend the already-delivered earlier messages** (Q120); delivery-state
transitions are persisted and monotonic.

### WS-11 — Cross-cutting verification
Only what cannot belong to a single workstream.

42. **E2E:** three fragmented messages → one batch → one workflow → one ordered reply in the fake client, asserted from the outside.
43. **Failure injection (§38):** kill the worker after commit but before ACK; on redelivery assert exactly one `outbound_message` and one workflow execution. Also duplicate outbox publish, Redis loss mid-window, broker disconnect during publish.
44. **Correlation:** the interaction `trace_id` minted by the aggregator appears in the workflow worker's and the dispatcher's log lines for the same interaction, and each contributing webhook request trace is reachable from it (Q131).
45. Testcontainers fixtures for Postgres/RabbitMQ/Redis, session-scoped so the suite stays fast enough for every PR (Q158).

### WS-13 — Process entrypoints (added during the sprint)
51. One entrypoint per role under `app/entrypoints/`, one image that runs any of
    them, and compose services for all five behind a one-shot migration service.
52. `make demo`: post a fragmented message to the running stack and require the
    reply it should produce.

**Tests:** the dispatcher refuses to start in a deployed environment without a
real provider client; one stop event is shared by every consumer in a process,
because a handler per consumer means SIGTERM wakes only the last; the outbox
loop drains a burst without pacing itself and still stops when asked.

### WS-12 — Decision records
46. Write `doc/adr/` with a template that **requires** a traceability block: affected spec section, related DEC, originating Q numbers (§48).
47. ADR-001 modular monolith with worker entrypoints (DEC-015, Q151-Q160).
48. ADR-003 at-least-once + outbox + idempotency (DEC-005, Q24-Q25, Q111-Q120, Q130).
49. ADR-004 32 partitions + SAC + the frozen hash (DEC-006, Q23, Q114-Q115).
50. **ADR-011 (new, beyond §43):** debounce flush via delayed message rather than in-process timer (DEC-010, Q113). §43's list does not cover it because the spec mandates the guarantee, not the mechanism — and §48 requires the choice be recorded rather than left in code.

Remaining ADRs from §43 are written when their decision is actually taken.

## Definition of Done

Every item is mechanically verifiable — no "works on my machine". All verified
on 2026-08-19 against `main`; 509 tests, green in CI with containers.

- [x] `docker compose up` yields a system that accepts a webhook and produces a reply, from a clean clone. *Verified against the running stack — five application containers behind a one-shot migration service — by posting three signed webhooks and reading back one batch, one workflow execution and one dispatched reply, with the interaction trace present in three separate containers' logs. `make demo` is that check in executable form, and it fails when the dispatcher is stopped.*
- [x] `make test` passes: unit + integration + E2E, with containers, in CI.
- [x] No workstream was merged without the tests listed under it (`CLAUDE.md` rule 2).
- [x] Every workstream shipped on a correctly prefixed branch with its own PR — PRs #4-#16, plus hotfix #17.
- [x] Duplicate webhook delivery (same `external_message_id`) creates exactly one `messages` row.
- [x] Redelivery of a workflow message after a post-commit crash creates **no** second `outbound_message`.
- [x] A DLQ message replayed through the tooling produces no duplicate business effect (Q117).
- [x] Three messages inside the debounce window produce exactly one `message_batch` and one reply.
- [x] A message arriving at 9s into a 10s cap still respects the absolute window.
- [x] Partition assignment for a fixed set of user IDs matches the golden test — the hash contract is frozen.
- [x] Every outbox row reaches `PUBLISHED`, or is visibly stuck and alertable; none are silently lost.
- [x] Three fragments across three webhook requests produce exactly **one** interaction trace, minted at batch persistence and reaching the dispatcher's logs, with all three request traces linked to it (Q131).
- [x] Each service role is refused the writes it should not have, asserted by test (Q145) — and exercised with that role's own credentials, not the admin's.
- [x] No BSUID, phone number or secret appears in any log line (asserted, not assumed).
- [x] Startup fails fast and legibly on missing or invalid configuration.
- [x] `mypy --strict` clean on `src/app`; `ruff` clean.
- [x] ADR-001, ADR-003, ADR-004 and ADR-011 committed, each with a §48 traceability block.

## What the implementation changed

Deviations and additions worth carrying forward, all of them found by a test or
a review rather than by planning:

| Change | Why |
| --- | --- |
| Migrations 0003 and 0004, beyond the planned 0001-0002 | Audio messages needed a `provider_media_id` or speech-to-text would have had nothing to fetch; and "one active conversation per user" and "one batch per message" became database invariants after two concurrency findings |
| Role and grant provisioning moved out of migration 0002 into `app/infrastructure/postgres/provisioning.py`, exposed as `make provision` | A migration runs once and is stamped forever, so a rotated password never reached PostgreSQL and an edited grant reached only fresh databases |
| Batch membership derived from `messages` rather than from a Redis list | Holding membership in Redis made it authoritative in practice, contradicting §10; the flush now sweeps the conversation's unbatched messages |
| `DomainEventEnvelope.from_message` as the only door into a consumer | Both workers had accepted a flattened payload the outbox never publishes, and the fixtures manufactured that shape, so the tests were complicit |
| Per-message transactions and a provider idempotency key in the dispatcher | A crash between "the provider accepted" and "the row says so" would otherwise resend, breaking Q120 exactly where it matters |
| `disable_existing_loggers=False` in the Alembic environment | Running a migration in-process silently switched off every application logger |
| Secrets read from the dotenv file the settings already use | `.env.example` documented secrets that the provider never read from a file, so a clean clone could not boot |
| WS-13: process entrypoints, an application image and compose services for all five roles | The plan's workstreams built every component but no process to run them: compose started only infrastructure, and the e2e suite constructed the pipeline in-process. Review of this closeout caught the acceptance criterion being marked done on that basis |

## Decisions needed

Resolve at the start; record as ADRs where structural.

| # | Decision | Default if unspecified |
| --- | --- | --- |
| D1 | Dependency manager: `uv` / Poetry / pip-tools | **`uv`** — fastest, lockfile, composes well with the Nix venv already in the repo |
| D2 | Async or sync SQLAlchemy | **Async** — the system is I/O-bound workers throughout; converting later is invasive |
| D3 | RabbitMQ client: `aio-pika` / `pika` | **`aio-pika`**, following D2 |
| D4 | Delayed-message mechanism: `rabbitmq_delayed_message_exchange` plugin vs per-tier TTL+DLX queues | **TTL + DLX queues** — no plugin dependency, works on any managed broker |
| D5 | UUIDv7 or UUIDv4 for PKs | **UUIDv7** — time-ordered, materially better index locality on append-heavy tables like `messages` |
| D6 | Is a real Meta WhatsApp number available for testing? | Assume **no**; fake client only, real integration deferred |
| D7 | Cloud target | Defer — Q160 keeps the core design vendor-neutral |
| D8 | Keep `whatsapp_training_ai_architecture_v1.0.md` alongside v1.1? | **Recommend deleting v1.0** — §1–45 are byte-identical, git history preserves it, and two near-identical 1200-line specs guarantee someone eventually reads the wrong one |

## Risks

- **Debounce correctness is the subtlest thing in this sprint.** Sliding window, absolute cap and
  generation counter interact, and the failure mode is silent message loss or split batches.
  Mitigation: unit-test the generation logic as a pure function before wiring it to Redis.
- **Single Active Consumer plus 32 partitions is easy to misconfigure** in a way that only appears
  under concurrent load from one user. Mitigation: an integration test driving two concurrent
  messages for one user and asserting serialized processing.
- **Per-service DB roles (Q145) will slow down local development** if the grants are wrong —
  failures surface as confusing permission errors deep in a worker. Mitigation: the grant tests in
  WS-3 run early and state the expected denial explicitly.
- **The stub handler is a temptation.** It must stay trivial. Any domain logic that leaks into it
  has to be moved in Sprint 2 — write it as an obviously-temporary seam.
- **Scope creep toward the workout domain.** The moment `exercise` appears in this sprint's diff,
  the sprint is off-plan.

## Hand-off to Sprint 2

Sprint 1 ends with a durable, observable, idempotent pipeline whose only weakness is that it has
nothing intelligent to say. Sprint 2 replaces the stub handler with the **deterministic** workout
domain — activity model, `ActivitySchemaRegistry`, `ActivityValidator`, exercise catalog and the
non-LLM stages of the resolver (§14, §16, Q41-Q51) — still with no LLM, driven by hand-written
structured input. That keeps domain correctness falsifiable before extraction quality becomes a
variable, which is DEC-001 applied to the build order itself.
