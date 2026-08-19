# ADR-001 — Modular monolith with distributed worker entrypoints

**Status:** Accepted
**Date:** 2026-08-19

## Traceability

| | |
| --- | --- |
| **Spec sections** | §5, §6, §37.1, §37.3 |
| **Decision records** | DEC-015 |
| **Interview questions** | Q151-Q160 |
| **Sprint / workstream** | Sprint 1, WS-1 |

## Context

The system has nine deployment roles (§5): an API, a message aggregator, a
workflow worker, an outbox publisher, a dispatcher, and four background
workers. They have genuinely different scaling profiles — the API is bursty and
cheap, workflow workers are the expensive ones, and the outbox publisher is
almost idle.

Different scaling profiles are the usual argument for separate services. But
they share a domain model, an event envelope, a schema and a set of ports, and
the product has not yet been built once. Splitting a domain nobody has
validated across repositories means paying for network boundaries, versioned
contracts and independent deploys while the boundaries are still moving.

## Decision

One repository, one importable package (`src/app`), one image, several
entrypoints. Modules are bounded by the §6 layout — `domain/`, `application/`,
`infrastructure/`, `workers/`, `api/` — and processes are selected at startup,
not at build time.

Communication between roles is RabbitMQ where it crosses a process boundary and
a plain Python call where it does not. HTTP between internal components is
forbidden: it would buy the costs of microservices without the isolation.

## Consequences

**Bought.** Independent scaling per role without independent deployment. One
schema migration path. A refactor that moves a boundary is an ordinary code
change rather than a cross-repository negotiation. New workers are new
entrypoints, not new infrastructure.

**Paid.** Nothing stops a careless import from reaching across a boundary — the
compiler will not object to `domain/` importing SQLAlchemy. A crash in shared
code affects every role. The image is larger than any single role needs, and
every role redeploys when any of them changes.

## Alternatives considered

**Separate services per role.** Rejected for now: the boundaries are still
moving, and a wrong boundary is far more expensive across repositories than
inside one. Q152 also rules out Kubernetes for the MVP, which removes most of
the operational upside.

**A single process running everything.** Rejected: §37.3 requires workflow
workers to scale on queue depth and oldest-message age independently of API
traffic, and one process makes the expensive role hostage to the cheap one.

## How this is enforced

`tests/unit/test_project_layout.py` parses §6 out of the specification and
asserts the package tree matches it, so drift from the declared structure fails
in CI rather than in review.

`tests/unit/test_persistence_contracts.py::test_domain_layer_does_not_know_about_sqlalchemy`
walks every module under `domain/` and fails if one imports SQLAlchemy, Alembic
or asyncpg — the boundary that a modular monolith most easily loses.
