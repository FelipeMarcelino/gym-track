# Architecture Decision Records

One file per decision, numbered to match §43 of the architecture spec where the
decision appears there. ADR-011 through ADR-014 are beyond §43's list: in each
case the spec mandates a *guarantee* but not the mechanism, and §48 requires
the choice be recorded rather than left in code.

Every ADR carries the traceability block from [`template.md`](template.md), and
`tests/unit/test_decision_records.py` fails if one does not — §48 is a rule
about this repository, so the repository is what checks it.

| ADR | Decision | Status |
| --- | --- | --- |
| [ADR-001](adr-001-modular-monolith.md) | Modular monolith with distributed worker entrypoints | Accepted |
| [ADR-003](adr-003-at-least-once-outbox.md) | At-least-once delivery, transactional outbox and idempotent effects | Accepted |
| [ADR-004](adr-004-workflow-partitions.md) | 32 stable-hash workflow partitions with Single Active Consumer | Accepted |
| [ADR-011](adr-011-debounce-flush-mechanism.md) | Debounce flush by delayed message rather than in-process timer | Accepted |
| [ADR-012](adr-012-deterministic-exercise-resolution.md) | Exercise resolution stops at the deterministic stages, and refuses rather than guesses | Accepted |
| [ADR-013](adr-013-strict-syntax-adapter.md) | The strict-syntax adapter is a testing seam with a fixed grammar, deleted in Sprint 3 | Accepted |
| [ADR-014](adr-014-versioned-derived-metrics.md) | Derived metrics are versioned pure functions, and no value is stored without its version | Accepted |

ADR-002 and ADR-005 through ADR-010 are written when their decisions are
actually taken. Writing them now would document intentions rather than
decisions, and an ADR nobody acted on is indistinguishable from a plan.
