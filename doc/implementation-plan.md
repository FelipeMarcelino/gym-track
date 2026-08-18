# Implementation Plan

Derived from `whatsapp_training_ai_architecture_v1.0.md` (the normative architecture spec).
This file is the **index**. Each sprint is detailed in its own file under `doc/sprints/`
and is written **one at a time**, immediately before it starts — not all upfront.

## Working model

- **Solo developer, no fixed timebox.** A sprint is a *logical unit of delivery*: a coherent,
  demonstrable increment that leaves the system in a working state. Sprints are sized by
  technical coherence, not by hours.
- **Detail just-in-time.** Only the current sprint is specified in depth. Later sprints exist
  here as named intent, derived from spec §41, and are explicitly provisional — they will be
  rewritten based on what the previous sprint teaches us.
- **The spec is normative, the plan is not.** Where this plan and the architecture spec
  disagree, the spec wins, or the spec gets an ADR amending it.

## Sprint list

| # | Sprint | Status | Detail |
| --- | --- | --- | --- |
| 1 | Walking skeleton — inbound to outbound, no intelligence | **Planned** | [sprint-01](sprints/sprint-01-walking-skeleton.md) |
| 2 | Workout logging domain + deterministic exercise resolver | Provisional | — |
| 3 | LangGraph MainGraph, ExecutionPlan, clarification interrupts | Provisional | — |
| 4 | WorkoutExtractor (LLM), EffortNormalizer, ResponseNormalizer | Provisional | — |
| 5 | Correction, undo, provenance, optimistic concurrency | Provisional | — |
| 6 | Analytics + TrainingAnalysisAgent | Provisional | — |
| 7 | RecommendationAgent + validator + critic | Provisional | — |
| 8 | RAG: knowledge registry, ingestion, retrieval | Provisional | — |
| 9 | Workout programs + long-term memory | Provisional | — |
| 10 | Hardening: privacy, retention, failure injection, SLOs | Provisional | — |

Sprints 2–10 are a **restatement of spec §41 phases**, not a commitment. They are here so the
shape of the whole is visible; only the row marked **Planned** is trustworthy.

## Sequencing rationale

Spec §41 Phase 0 is pure foundation with no user-visible behavior. We deliberately deviate:
Sprint 1 keeps every Phase 0 item but threads them onto a **thin vertical slice**, so the
architectural spine (durability, idempotency, at-least-once delivery, correlation) is exercised
by real traffic from the first increment instead of being validated months later.

The ordering invariant for everything after: **deterministic before probabilistic.** The
workout domain, its validators and its persistence are built and tested with hand-written
inputs (Sprint 2) *before* an LLM is allowed to produce those inputs (Sprint 4). This follows
spec §3.1 — probabilistic components propose, deterministic services validate and commit — and
means LLM quality problems can never be confused with domain correctness problems.
