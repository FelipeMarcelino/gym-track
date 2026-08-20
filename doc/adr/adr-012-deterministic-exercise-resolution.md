# ADR-012 — Exercise resolution stops at the deterministic stages, and refuses rather than guesses

**Status:** Accepted
**Date:** 2026-08-20

## Traceability

| | |
| --- | --- |
| **Spec sections** | §16, §15 |
| **Decision records** | DEC-001 |
| **Interview questions** | Q41, Q42 |
| **Sprint / workstream** | Sprint 2, WS-7 |

## Context

§16 defines a staged resolution hierarchy — the user's own aliases, the global
ones, the canonical names, fuzzy matching, then vector search and an LLM — and
Q42 requires the outcome to be routed by confidence. Sprint 2 has no model in
the loop and no vector index, so it can implement the first four stages and
nothing else.

That leaves the question the sprint actually has to answer: what happens when
the deterministic stages are not confident. A logging assistant that resolves
"agachamento bulgaro" to the back squat is not slightly wrong. It writes a row
that looks correct, joins cleanly, and appears in the user's history forever as
training they did not do. DEC-001 puts authority in deterministic components
precisely so that this class of error is impossible to introduce quietly.

The measurement that forced the thresholds: the plan named `rapidfuzz.WRatio`,
which falls back to a partial ratio when the strings differ in length. Against
the seeded catalog, "agachamento bulgaro" scored **0.90** against "Agachamento
livre" and "supino com corda" scored **0.90** against "Supino reto" — both
above any reasonable write threshold, both a different exercise.

## Decision

Resolution runs stages 1–4 in §16's order, first hit wins, and each stage is
consulted only when the one before it comes back empty.

Fuzzy scoring uses **`token_sort_ratio`**, not `WRatio`: it ignores word order,
which pt-BR typing needs ("terra levantamento"), while still charging for the
words that differ. The two cases above drop to 0.78 and 0.69; the misspellings
this stage exists for ("supino retp", "levantamento tera") stay at 0.91–0.97.

Confidence routes the outcome (Q42):

| Top score | Outcome |
| --- | --- |
| ≥ 0.90, clear of the runner-up by more than 0.02 | resolved, `method=FUZZY` |
| ≥ 0.90, runner-up within 0.02 | **not** resolved; both candidates returned, clarification required |
| 0.70 – 0.90 | not resolved; candidates offered, clarification not demanded |
| < 0.70 | not resolved; **no candidates at all** |

`ResolutionMethod` declares `VECTOR`, `LLM` and `USER_CONFIRMED`, and this
sprint never returns them: the stored vocabulary is fixed now so the sprints
that add those stages are additive rather than a migration of meaning.

## Consequences

The system asks more questions than a greedier matcher would, and some of those
questions will annoy a user whose typo was obvious to them.

**Paid.** Every unresolved name costs a round trip, and the 0.70 floor means a
genuinely novel exercise gets no suggestions at all — the user is asked what it
is called rather than offered the nearest thing in the catalog. That is
deliberate: offering the best of a bad list invites the user to confirm
something the system invented, and a confirmed wrong exercise is indistinguishable
from a correct one afterwards.

**Paid.** The thresholds are tuned against a 46-exercise seed. They will need
re-measuring when the catalog grows, and the golden fixture is what will make
that visible.

**Bought.** A wrong exercise cannot enter the history through this path without
a human confirming it, which is what makes the derived metrics and the training
history worth computing at all.

## Alternatives considered

**`WRatio` with a higher threshold.** Raising the bar to 0.95 would have
suppressed the two bad matches, but WRatio's partial-ratio fallback means the
score does not measure what it appears to: a short query inside a long name
scores as an exact match regardless of the threshold. Tuning a number to work
around a scorer that answers a different question is how a system becomes
untunable.

**`token_set_ratio`.** Scored "agachamento bulgaro" against "Agachamento livre"
at **1.00** — the worst possible answer, confidently.

**Resolving to the best candidate and letting the user correct it later.**
Rejected: corrections are Sprint 4, and even with them, a wrong row that nobody
notices is never corrected. The asymmetry is the whole point — an unresolved
name asks a question and gets an answer, while a wrong one is silently wrong
forever.

## How this is enforced

`tests/application/test_exercise_resolver.py` asserts the *absence*:
`test_a_weak_match_resolves_to_nothing_rather_than_to_something` requires
`exercise_id is None` and an empty candidate list, and
`test_two_close_matches_ask_instead_of_picking` requires a question rather than
a pick when two candidates sit within the margin.

`tests/domain/fixtures/exercise_resolution.json` freezes 35 rows of real input
against the scorer pinned in `uv.lock`. Five of them are explicit nulls —
including "agachamento bulgaro" and "supino com corda", the two that motivated
this ADR — so a scorer change that reintroduces the failure fails CI rather
than a user's history.

A test asserts no fixture row is ever resolved by a method in
`UNIMPLEMENTED_METHODS`.
