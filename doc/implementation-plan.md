# Implementation Plan

Derived from `whatsapp_training_ai_architecture_v1.1.md` (the normative architecture spec).
v1.1 keeps §1–45 byte-identical to v1.0 and adds the traceability appendix: §46 (Q1–Q160),
§47 (DEC-001..015) and §48 (traceability maintenance rule).
This file is the **index**. Each sprint is detailed in its own file under `doc/sprints/`
and is written **one at a time**, immediately before it starts — not all upfront.

## Working model

- **Solo developer, no fixed timebox.** A sprint is a *logical unit of delivery*: a coherent,
  demonstrable increment that leaves the system in a working state. Sprints are sized by
  technical coherence, not by hours.
- **Detail just-in-time.** Only the current sprint is specified in depth. Later sprints exist
  here as named intent, derived from spec §41, and are explicitly provisional — they will be
  rewritten based on what the previous sprint teaches us.
- **Plan before code, tests with code.** Per `CLAUDE.md`: no implementation starts without a
  written plan for it, and no implementation lands without tests covering it. At sprint level the
  plan is the sprint file; at task level it is a short plan agreed before the first line of code.
  Tests belong to the workstream that introduces the behavior, not to a cleanup pass at the end.
- **The spec is normative, the plan is not.** Where this plan and the architecture spec
  disagree, the spec wins, or the spec gets an ADR amending it.
- **Branch and PR per unit of work.** Per `CLAUDE.md`: features on `feat/<name>`, bugfixes on
  `hotfix/<name>`, documentation on `doc/<name>`, each merged to `main` through a PR opened with
  `gh`. A sprint is not a branch — each workstream inside it is.
- **Every ADR carries traceability.** Per spec §48, an ADR links back to the affected spec
  section and, where applicable, the originating Q decision and DEC record. Architectural changes
  are captured as ADRs rather than introduced implicitly in code.

## Sprint list

| # | Sprint | Status | Detail |
| --- | --- | --- | --- |
| 1 | Walking skeleton — inbound to outbound, no intelligence | Done | [sprint-01](sprints/sprint-01-walking-skeleton.md) |
| 2 | Workout logging domain + deterministic exercise resolver | Done | [sprint-02](sprints/sprint-02-deterministic-workout-domain.md) |
| 3 | LangGraph MainGraph, ExecutionPlan, clarification interrupts | **Planned** | [sprint-03](sprints/sprint-03-langgraph-orchestration.md) |
| 4 | The LLM boundary: IntentRouter, ExecutionPlanner, WorkoutExtractor, ResponseNormalizer | Provisional | — |
| 5 | Correction, undo, provenance, optimistic concurrency | Provisional | — |
| 6 | Analytics + TrainingAnalysisAgent | Provisional | — |
| 7 | RecommendationAgent + validator + critic | Provisional | — |
| 8 | RAG: knowledge registry, ingestion, retrieval | Provisional | — |
| 9 | Workout programs + long-term memory | Provisional | — |
| 10 | Hardening: privacy, retention, failure injection, SLOs | Provisional | — |

Sprints 4–10 are a **restatement of spec §41 phases**, not a commitment. They are here so the
shape of the whole is visible; only the row marked **Planned** is trustworthy, and the rows marked
**Done** are what actually shipped rather than what was once intended.

Sprint 3 keeps the ordering invariant one level up: the graph, the plan DAG, the checkpointer and
the interrupt/resume path are all deterministic, and they are built and falsified **before** an LLM
produces anything that travels through them. Sprint 2's file closed by naming Sprint 3 as the LLM
boundary; this index is the authority, and that paragraph has been corrected.

## Sequencing rationale

Spec §41 Phase 0 is pure foundation with no user-visible behavior. We deliberately deviate:
Sprint 1 keeps every Phase 0 item but threads them onto a **thin vertical slice**, so the
architectural spine (durability, idempotency, at-least-once delivery, correlation) is exercised
by real traffic from the first increment instead of being validated months later.

The ordering invariant for everything after: **deterministic before probabilistic.** The
workout domain, its validators and its persistence are built and tested with hand-written
inputs (Sprint 2), and the orchestration that carries them — graph, plan, checkpoints, interrupts —
is built and tested with deterministic inputs (Sprint 3), *before* an LLM is allowed to produce
those inputs (Sprint 4). This follows spec §3.1 — probabilistic components propose, deterministic
services validate and commit — and means LLM quality problems can never be confused with domain
correctness problems, nor with orchestration bugs.
