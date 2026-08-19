# Sprint 2 — Deterministic Workout Domain

**Status:** Planned
**Spec basis:** v1.1 — §7.3, §14, §15 (deterministic half), §16 (stages 1-4), §18, §26, §28, §38
**Decision records exercised:** DEC-001, DEC-005, DEC-007 (partially), DEC-015
**Depends on:** Sprint 1 — the walking skeleton, merged and running (`make up`, `make demo`)

## Goal

The pipeline stops acknowledging and starts **understanding**, without a single
LLM call:

```text
structured workout input
        -> exercise resolution (deterministic stages)
        -> unit and effort normalization
        -> ActivityValidator
        -> TrainingSessionManager
        -> one transaction: session, exercises, sets, provenance, events, outbox
        -> a deterministic confirmation
```

Sprint 1 proved that a message survives every crash window. Sprint 2 makes the
thing it survives *mean something*: a set of eight repetitions at eighty kilos
becomes rows a query can answer questions about.

**Zero LLM in this sprint.** No extractor, no LLM-assisted resolution, no
prose normalizer. This is DEC-001 applied to the build order: business
correctness belongs to validators, services, transactions and constraints, and
all of those must be falsifiable *before* extraction quality becomes a variable
that can hide their bugs.

**Demo at end of sprint:** send `#log supino 80kg 10 9 8` and observe one
training session, one session exercise, three sets with an inherited load whose
provenance says so, and a confirmation naming what was recorded. Then send
`#log supino 80kg` and observe a clarification request instead of an invented
repetition count.

## Why an input contract before an extractor

The extractor is a Sprint 3 concern, but the domain cannot be built against
nothing. So this sprint defines `StructuredWorkoutInput` — **the contract the
extractor will be required to emit** — and drives the domain through it.

Two consequences worth stating up front:

- The contract is frozen by a golden fixture in `tests/contract/`, the same way
  the domain event envelope is. When Sprint 3 writes the extractor, its job is
  defined by a file rather than by reading the domain's implementation.
- Something has to produce that contract from a WhatsApp message, or nothing is
  verifiable end to end. WS-10 adds a **strict-syntax adapter** — a
  deliberately rigid, deterministic parser — which exists to be deleted when
  the extractor lands. It is a testing seam that happens to be usable.

## Scope

### In

| Area | What ships | Traceability |
| --- | --- | --- |
| Catalog schema | `exercises`, `exercise_aliases`, `muscles`, `exercise_muscles`, `equipment`, `exercise_equipment`, `exercise_relations` | §16, §26, Q43, Q44 |
| Catalog seed | A small curated canonical catalog with pt-BR and en aliases | §16 |
| Activity model | `ActivitySchemaRegistry`, `ActivityValidator`, the four validation outcomes | §14.1, Q45 |
| Strength semantics | Load modes, set types, per-implement default, bodyweight ± | §14.2, Q46, Q48-Q51 |
| Units and metrics | Deterministic normalization, pace/speed, volume, estimated 1RM, all versioned | §14.1, Q47, Q52 |
| Effort | `EffortNormalizer`, deterministic half only: RPE, RIR mapping, a fixed pt-BR phrase table | §14.3 |
| Sessions | `training_sessions`, `TrainingSessionManager`, lazy expiry plus `session-expiration-worker` | §18, Q31, Q32 |
| Workout schema | `session_exercises` with `exercise_block_index`, `exercise_sets`, `exercise_groups`, `entity_sources` | §26, §26.2, Q51, Q54, Q58 |
| Resolver | Stages 1-4 (user alias, global alias, canonical, fuzzy) with confidence routing | §16, Q41, Q42 |
| Input contract | `StructuredWorkoutInput`, frozen by a golden fixture | §15, Q56 |
| Commands | `WorkoutCommandBuilder`, `LogWorkoutCommand`, `WorkoutApplicationService` | §15, Q56, Q57 |
| Handler | `LOG_WORKOUT` replaces the stub in the §11.3 registry | §11.3, DEC-001 |
| Input adapter | Strict-syntax parser, explicitly temporary | — |
| ADRs | ADR-012, ADR-013, ADR-014 with §48 traceability blocks | §48 |

### Explicitly out

LLM providers, the `WorkoutExtractor` and every prompt · LangGraph and the
`WorkoutLoggingSubgraph` · `ExecutionPlanner` and the task DAG · clarification
**interrupts** (this sprint *detects* the need and answers deterministically;
the interrupt machinery is Sprint 3) · corrections and undo (§17) · vector and
LLM resolver stages 5-6 (§16) · analytics (§19) · recommendations · programs ·
memory · RAG and Qdrant · `ResponseNormalizerAgent` (§25) — replies stay
deterministic templates · real Meta WhatsApp integration.

**Deliberate deviation from §15:** the subgraph diagram routes ambiguity into a
LangGraph interrupt. Without LangGraph, an ambiguous item here produces a
`DomainResult` asking for the missing datum and persists nothing for that item.
The *behaviour* the user sees is the same; the mechanism arrives with the
graph. Recorded so the gap is visible rather than silent.

**Deliberate deviation from §14.3:** `EffortNormalizer` gets its deterministic
half only. An unrecognised effort phrase is stored raw and left unnormalized —
never guessed. The small LLM classifier fallback joins in Sprint 3.

## Work breakdown

Ordered by dependency. Each workstream is independently reviewable, **ships its
own tests**, and leaves the tree green. Per `CLAUDE.md`: `feat/` branches for
implementation, `doc/` for documentation-only, `hotfix/` for defects found
after a merge, one PR each, reviewed and CI-green before merging.

### WS-1 — Exercise catalog schema and seed
1. Migration: the seven catalog tables, with `UNIQUE(canonical_name)` on
   exercises and alias uniqueness scoped by `(user_id, normalized_alias)` —
   a global alias uses a null `user_id`.
2. `exercise_muscles.role` as PRIMARY/SECONDARY/STABILIZER (Q43);
   `exercise_relations.type` as VARIATION_OF/SUBSTITUTE_FOR/SIMILAR_MOVEMENT/
   PROGRESSION_OF/REGRESSION_OF (Q44).
3. A curated seed: roughly forty exercises covering the movements a beginner
   logs, each with pt-BR and en aliases, muscles and equipment.
4. Seeding is idempotent and re-runnable, like role provisioning.
5. Grants for the new tables per service role (Q145).

**Tests:** the seed applies twice with the same result; a duplicate canonical
name is refused; a global and a user alias may share text while two global
aliases may not; every seeded exercise has at least one PRIMARY muscle; every
alias normalizes to something the resolver can match; migration up and down.

### WS-2 — Activity model and validator
6. `ActivityType`, `LoadMode`, `SetType` as domain enums (Q45, Q49, Q50).
7. `ActivitySchemaRegistry`: what each activity type requires, as data.
8. `ActivityValidator` returning VALID, VALID_WITH_WARNINGS,
   MISSING_ESSENTIAL_DATA or INVALID, with the offending field named.

**Tests:** table-driven per activity type — a strength set without reps is
MISSING_ESSENTIAL_DATA and says *reps* (Q46); `10 flexões` is VALID with no
load (Q48); a distance activity is VALID with distance only, duration only or
both (Q47); a negative or absurd value is INVALID rather than a warning; the
registry covers every `ActivityType` member, asserted by iterating the enum so
a new type cannot be added without a schema.

### WS-3 — Units and derived metrics
9. Canonical storage units — kilograms, meters, seconds — with parsing for
   kg/lb, km/mi/m and min/s.
10. Derived metrics as versioned pure functions: pace, speed, volume,
    estimated 1RM (Q52).
11. `metric_version` persisted alongside every derived value.

**Tests:** a golden conversion table including the boundaries people actually
type (`80kg`, `80 kg`, `176lb`, `5k`, `5 km`, `1500m`, `1:30`, `90s`); pace is
derived only when distance *and* duration exist and is absent otherwise rather
than zero; 1RM matches a frozen table of hand-computed values; changing a
formula without bumping its version fails a test.

### WS-4 — Effort normalization (deterministic half)
12. `EffortNormalizer`: explicit RPE passes through, RIR maps by a fixed table,
    and a curated pt-BR phrase table maps common expressions.
13. Persist raw effort, normalized RPE, method and version.
14. Activity-level effort is stored on the activity and **not** copied onto
    every set (§14.3).

**Tests:** the RIR table is asserted value by value; an unrecognised phrase
yields raw stored and normalized null — the test asserts *nothing was invented*;
effort stated once for a whole exercise does not appear as a per-set RPE; method
and version are recorded for every normalization.

### WS-5 — Training sessions
15. `training_sessions` and `TrainingSessionManager`: start on first log when no
    valid active session exists, refresh `last_activity_at`, close after the
    configured inactivity (§18).
16. Lazy expiry on the next workout input, plus a `session-expiration-worker`
    entrypoint and its compose service.
17. PostgreSQL `last_activity_at` is authoritative; a Redis hint may only
    *suggest* expiry (§18).
18. No retroactive sessions: `performed_at` belongs to the current interaction
    (§7.3, Q32).

**Tests:** first log opens a session; a second log inside the timeout reuses it;
a log after the timeout closes the old one and opens a new one, asserted through
the lazy path *and* through the worker; a stale Redis hint claiming expiry does
not close a session PostgreSQL says is active; concurrent logs for one user do
not open two sessions (the WS-7 lesson from Sprint 1, applied here as a partial
unique index).

### WS-6 — Workout domain schema
19. Migration: `session_exercises` (with `exercise_block_index`), `exercise_sets`,
    `exercise_groups`, `entity_sources`, all soft-deletable where §26 says so.
20. `provenance` on values that can be inherited: EXPLICIT or INHERITED (§14.4).
21. `expected_version` on mutable rows, so Sprint 4's corrections have optimistic
    concurrency to build on (§17).
22. `entity_sources` links every created row to the message or batch it came
    from (§26.2).

**Tests:** consecutive sets of one exercise share a block; returning to that
exercise after another creates a second block with a higher index (Q58); sets
keep their order within a block; every persisted set is reachable from its source
message through `entity_sources`; a soft-deleted set leaves the block's ordering
intact.

### WS-7 — Exercise resolver, deterministic stages
23. Stages in the normative order: exact user alias, exact global alias,
    canonical name, normalized lexical/fuzzy (§16 items 1-4).
24. Normalization for matching: casefold, strip accents, collapse whitespace and
    punctuation.
25. `ExerciseResolution` with `raw_name`, `exercise_id`, `canonical_name`,
    `method`, `confidence`, `candidates[]` and `requires_clarification`.
26. Confidence routing (Q42): high resolves, medium returns candidates, low or
    ambiguous sets `requires_clarification`. Stages 5-7 are declared and
    explicitly unimplemented.

**Tests:** a **golden table of raw names to canonical exercises**, in pt-BR,
including accents and common misspellings — the resolver's contract with real
input; the order is asserted by constructing a case where a user alias and a
global alias disagree and the user alias must win; a fuzzy match below the
threshold does **not** resolve, because silently logging the wrong exercise is
the worst failure this system can have; two equally-scored candidates set
`requires_clarification` rather than picking the first.

### WS-8 — Input contract and command builder
27. `StructuredWorkoutInput`: the extractor's future output, as a typed model.
28. Golden fixture freezing it, as a contract test.
29. Fragment inheritance: one stated load applied to several rep counts, with
    provenance recorded per value (§14.4, Q54).
30. `WorkoutCommandBuilder` producing a `LogWorkoutCommand`; ambiguous items are
    excluded from the command before it is built, valid ones are kept (Q56).

**Tests:** the golden fixture round-trips and rejects unknown fields; `80 kg:
10, 9, 8` yields three sets, one EXPLICIT load and two INHERITED; a command
containing one resolvable and one ambiguous exercise carries only the first and
reports the second; an empty command is refused rather than committed.

### WS-9 — Application service and the LOG_WORKOUT handler
31. `WorkoutApplicationService`: one transaction for domain rows,
    `entity_sources`, `domain_events` and `outbox_events` (§15, Q57).
32. Idempotency by `operation_id` through `processed_operations` (§28), so a
    redelivered batch cannot log the same sets twice.
33. Register `LOG_WORKOUT` in the §11.3 handler registry, replacing the stub for
    inputs that parse as workouts.
34. A deterministic confirmation naming what was recorded, and a deterministic
    clarification request when something essential is missing.

**Tests:** redelivery of the same batch produces no second set and no second
event — asserted on row counts, not on a return value; a handler failure leaves
nothing behind; the confirmation names the exercise and the set count; the
clarification names the missing field; `mypy --strict` still clean with the
domain in place.

### WS-10 — Strict-syntax input adapter (temporary)
35. A rigid parser for a documented syntax (`#log <exercise> <load> <reps...>`),
    producing `StructuredWorkoutInput`.
36. Anything that does not match falls through to Sprint 1's acknowledgement
    handler — an unparsed message is never silently dropped.
37. Documented in the README as temporary, with the sprint that removes it.

**Tests:** contract tests over the syntax including its failure modes; a
non-matching message still gets the acknowledgement; the adapter never invents a
value the syntax did not contain.

### WS-11 — Cross-cutting verification
38. **E2E:** a WhatsApp message in the strict syntax produces persisted sets and
    a confirmation, asserted from outside through the running stack.
39. **E2E:** an incomplete message produces a clarification and persists no sets.
40. **Failure injection (§38):** redelivery after commit, duplicate publication,
    and a crash between the domain write and the outbox write (which must be
    impossible — same transaction — and the test proves it).
41. `make demo` extended to the workout path, failing when the domain does not
    persist what it should.

### WS-12 — Decision records
42. **ADR-012** — exercise resolution stops at deterministic stages this sprint,
    with the chosen confidence thresholds and why silence beats a wrong guess
    (§16, Q41, Q42).
43. **ADR-013** — the strict-syntax adapter as a temporary seam: what it is for,
    what it must never grow into, and when it is removed.
44. **ADR-014** — derived metrics are versioned deterministic code paths
    (§14.1, Q52, DEC-001).

## Definition of Done

Every item is mechanically verifiable — no "works on my machine".

- [ ] `make demo` logs a workout through the running stack and fails if the sets are not persisted.
- [ ] `make check` passes: unit, domain, contract, integration and E2E, with containers, in CI.
- [ ] No workstream merged without the tests listed under it, on a correctly prefixed branch with its own reviewed PR.
- [ ] `supino 80kg 10 9 8` produces one session, one exercise block and three sets, with the load EXPLICIT on the first and INHERITED on the rest.
- [ ] `supino 80kg` produces no sets and a clarification naming *repetitions* (Q46).
- [ ] `10 flexões` produces a valid set with no load (Q48).
- [ ] A dumbbell load of 20 kg is stored as PER_IMPLEMENT without being asked (Q49).
- [ ] Returning to an exercise after another creates a second block, and the two blocks' indices preserve workout order (Q58).
- [ ] A raw name matching a user alias resolves to that user's exercise even when a global alias says otherwise (§16 order).
- [ ] A fuzzy match below the threshold resolves to nothing and asks, rather than to the closest exercise.
- [ ] Every persisted set is reachable from the message that created it through `entity_sources` (§26.2).
- [ ] Redelivery of a workflow message creates no second set, asserted on row counts.
- [ ] A session opens on the first log, is reused inside the timeout, and is closed by both the lazy path and the expiration worker.
- [ ] A Redis expiry hint cannot close a session PostgreSQL considers active (§18).
- [ ] An unrecognised effort phrase is stored raw and normalized to nothing — no invented RPE.
- [ ] Every derived metric records the version of the code that produced it (Q52).
- [ ] `StructuredWorkoutInput` is frozen by a golden fixture, so Sprint 3's extractor has a contract rather than an example.
- [ ] `mypy --strict` clean; `ruff` clean; no module under `domain/` imports SQLAlchemy.
- [ ] ADR-012, ADR-013 and ADR-014 committed, each with a §48 traceability block.

## Decisions needed

Resolve at the start; record as ADRs where structural.

| # | Decision | Default if unspecified |
| --- | --- | --- |
| D1 | Catalog seed: curated by hand or imported | **Curated, ~40 exercises.** An imported catalog brings thousands of names nobody validated, and resolver quality is judged against names real users type |
| D2 | Fuzzy matching library | **`rapidfuzz`** — fast, well-tested, and its scores are stable enough to freeze in a golden test |
| D3 | Confidence thresholds | **≥0.90 resolves, 0.70-0.90 returns candidates, <0.70 asks.** Deliberately conservative: a wrong exercise silently logged is worse than a question |
| D4 | Storage units | **SI: kilograms, meters, seconds.** Display conversion is a presentation concern |
| D5 | Estimated 1RM formula | **Epley**, versioned as `1rm.epley.v1`, so a later change is a new version rather than a rewrite of history |
| D6 | Corrections (§17) in this sprint | **No.** They need entity reference resolution, which needs the LLM boundary; Sprint 4 |
| D7 | Strict syntax shape | **`#log <exercise> <load> <reps...>`**, prefix-marked so it can never collide with natural language the extractor will handle |
| D8 | Where clarification text comes from | **Deterministic templates** in the domain result; the normalizer that would rewrite them is Sprint 3 |

## Risks

- **A wrong exercise logged silently is the worst outcome in this sprint.** It
  corrupts history that later analysis will treat as fact. Mitigation: the
  conservative thresholds in D3, a golden resolution table, and a test asserting
  that a below-threshold match resolves to *nothing*.
- **The input contract could drift from what an LLM can actually produce**,
  which would make Sprint 3 an adaptation rather than an addition. Mitigation:
  the contract is shaped as the extractor's output and frozen by a fixture, and
  every field it carries must be one a model can plausibly emit from a sentence.
- **The strict-syntax adapter is a temptation.** It is a seam, not a feature.
  Any growth toward parsing natural language duplicates the extractor and will
  have to be deleted twice. ADR-013 states the boundary.
- **Fragment inheritance is subtle**, and its failure mode is quiet: a load
  inherited where it should not be produces plausible rows. Mitigation:
  provenance is stored per value, and tests assert INHERITED explicitly rather
  than only checking totals.
- **Scope creep toward analysis.** The moment a query computes a trend, the
  sprint is off-plan; volume and 1RM are stored, not interpreted.

## Hand-off to Sprint 3

Sprint 2 ends with a domain that is correct and completely literal: it records
exactly what it is told, in a rigid syntax nobody would want to type. Sprint 3
introduces the LLM boundary — `IntentRouter`, `WorkoutExtractor`, the
`MainGraph` and `ResponseNormalizer` — against a domain whose rules are already
falsifiable, so a bad extraction shows up as a validation failure rather than
as a plausible wrong row. The strict-syntax adapter is removed there, and its
contract test becomes the extractor's first eval case.
