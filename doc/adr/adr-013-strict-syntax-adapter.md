# ADR-013 — The strict-syntax adapter is a testing seam with a fixed grammar, and it is deleted in Sprint 3

**Status:** Accepted
**Date:** 2026-08-20

## Traceability

| | |
| --- | --- |
| **Spec sections** | §15, §11.3 |
| **Decision records** | DEC-001 |
| **Interview questions** | Q56 |
| **Sprint / workstream** | Sprint 2, WS-10 |

## Context

Sprint 2 builds the deterministic workout domain: resolution, units, effort,
validation, persistence, provenance and confirmation. Sprint 3 builds the
extractor that turns "fiz supino 3x10 com 80kg" into a structured input.

That ordering is deliberate — the domain has to be correct before anything
probabilistic feeds it — but it leaves Sprint 2 with a domain and no way to
reach it. Every test could construct a `StructuredWorkoutInput` by hand, but
then nothing would ever have produced one, and §38's end-to-end verification
would stop at the workflow worker.

Something has to speak the contract. The risk is what that something becomes:
a stand-in that starts accepting "3x10" because it is easy, then synonyms
because they are useful, then free text because it is nearly there — and by
Sprint 3 there are two extractors, one of which nobody chose to build.

## Decision

A rigid, positional syntax, and no more:

```text
#log <exercise words> [<load>] [<reps> ...] [@<effort>]
```

Every token type is distinguished by shape: a load carries a unit suffix, reps
are bare integers, an effort is prefixed with `@`. The prefix must be the whole
first token, so `#logger` is an ordinary hashtag.

The parser **infers nothing**. `#log supino 80kg` produces a set with no
repetitions, which is exactly what Q46 turns into a clarification. `3x10` is
refused by name. A line that carries the marker and does not parse is reported
back to the user rather than skipped, because they typed the marker and an
acknowledgement for a workout nobody recorded is worse than an error.

**This file is deleted in Sprint 3**, when the `WorkoutExtractor` lands. Its
contract test becomes that extractor's first eval case.

## Consequences

Sprint 2 can be exercised end to end over real infrastructure, and the strict
adapter is also the first proof that WS-8's contract can be produced at all
rather than only consumed.

**Paid.** Nobody can use this system naturally until Sprint 3. The syntax is
documented in the README so it is usable, but it is a developer's interface
wearing a user's clothes, and anyone who tries it will type "3x10" first and be
told no.

**Paid.** The deletion is a promise, not a mechanism. Nothing in CI fails when
Sprint 3 arrives and this file stays — the README sentence and this ADR are the
only things holding it.

**Bought.** Sprint 3's extractor is written against a contract that already has
a working producer and a passing eval case, instead of against a specification
nobody has satisfied.

## Alternatives considered

**Hand-built `StructuredWorkoutInput` objects in tests only.** Cheaper, and it
would have left §38's end-to-end tests unable to start from a webhook. A
pipeline verified from its middle outward is verified in exactly the places
where wiring mistakes do not live.

**A lenient parser that accepts `3x10` and common synonyms.** More useful today,
and the reason this ADR exists: each individual leniency is defensible, and the
sum of them is a second extractor that Sprint 3 has to reconcile with rather
than replace.

**Shipping Sprint 3's extractor first.** Rejected by the sprint ordering: a
probabilistic component feeding a domain that has not been proven correct makes
every failure ambiguous between the two.

## How this is enforced

`tests/contract/test_strict_syntax.py` refuses the slide directly:
`test_the_x_notation_is_refused_on_purpose` requires `3x10` to raise, and
`test_a_marked_line_that_does_not_parse_says_so` requires an error rather than
a silent skip.

`test_no_value_is_invented` is the mechanical form of the whole decision: for
every accepted line, every value in the produced contract must appear as a
substring of what the user typed. A default or a synonym leaking into this
parser fails that test by construction.

The README states the grammar and names the sprint that removes the file.
