# ADR-014 — Derived metrics are versioned pure functions, and no value is stored without its version

**Status:** Accepted
**Date:** 2026-08-20

## Traceability

| | |
| --- | --- |
| **Spec sections** | §14.1, §19 |
| **Decision records** | DEC-001 |
| **Interview questions** | Q52 |
| **Sprint / workstream** | Sprint 2, WS-3 |

## Context

Q52 puts volume, pace, speed and estimated 1RM in deterministic services rather
than in an LLM: they are arithmetic, and arithmetic a model performs is
arithmetic nobody can check.

Deciding *where* they are computed does not settle the harder problem. These
formulas change. Epley is one 1RM estimator among several, volume for a
per-implement load is a modelling choice rather than a fact, and a future
sprint will improve one of them. When that happens, the database holds numbers
produced by two different definitions, and every one of them looks identical:
a `NUMERIC` column with a plausible value in it.

A user asking "why did my volume drop" deserves a better answer than "the
formula changed at some point, possibly before this row".

## Decision

Each metric is a pure function with a version string that names the formula:
`volume.load_x_reps.v1`, `pace.seconds_per_km.v1`, `speed.meters_per_second.v1`,
`1rm.epley.v1`. Changing what a formula computes means a new version, never an
edit to an existing one.

Every stored derived value carries the version that produced it, in a paired
column — `volume_kg` with `volume_metric_version`, and so on. The pairing is a
**database CHECK constraint**:

```sql
CHECK ((volume_kg IS NULL) = (volume_metric_version IS NULL))
```

A number nobody can reproduce is worse than no number, so the database refuses
half a pair rather than trusting the service to write both.

`compute_by_version` dispatches on the version string, which is what makes a
stored row recomputable: given the version, the same inputs produce the same
output, forever.

## Consequences

Any question about a stored metric has an exact answer, and a formula change is
a migration of *meaning* that the schema forces someone to think about.

**Paid.** Four extra columns on `exercise_sets`, four CHECK constraints, and a
lookup table mapping versions to columns in the service. A new metric is not
one function — it is a function, a version, a column pair, a constraint, a map
entry and a fixture row.

**Paid.** The version strings are permanent. `1rm.epley.v1` will still be in
the database long after nobody uses Epley, and rows carrying it can never be
silently reinterpreted.

**Bought.** Recomputation is possible without archaeology, and mixed-version
history is legible rather than merely present.

## Alternatives considered

**A single global schema version on the row.** Simpler, and wrong at the first
change: bumping it would mark every metric as new when only one formula moved,
and a reader could no longer tell which value the change affected.

**Recomputing derived metrics on read instead of storing them.** Removes the
versioning problem by removing the stored value, and removes the history with
it — a user's past volume becomes whatever today's formula says it was, which
is precisely the rewriting this decision exists to prevent.

**Enforcing the pairing in the service only.** The service is where the pairing
is *produced*; it is not where it will be broken. A future writer that sets a
value and forgets the version passes review easily and fails a constraint
immediately.

## How this is enforced

`tests/contract/test_derived_metrics.py` runs a frozen fixture: known inputs,
known outputs, known versions. It has already caught a real arithmetic error —
480 s/mile is 298.258 s/km, not 298.259.

The CHECK constraints in migration `0007_workout_domain` are asserted by
`tests/integration/test_workout_schema.py::test_a_derived_value_cannot_be_stored_without_its_version`,
which requires an `IntegrityError` when a value arrives without its version —
parametrized over **all four pairs**, because a constraint no test exercises is
one a later migration can drop without anything noticing.

`WorkoutApplicationService._apply_metrics` writes through a version-to-column
map and raises on an unknown version, so a metric added without a column pair
fails at the point of writing rather than at the constraint.
