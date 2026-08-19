# ADR-011 — Debounce flush by delayed message rather than in-process timer

**Status:** Accepted
**Date:** 2026-08-19

## Traceability

| | |
| --- | --- |
| **Spec sections** | §8, §9.4, §10 |
| **Decision records** | DEC-010 |
| **Interview questions** | Q11, Q113, Q116 |
| **Sprint / workstream** | Sprint 1, WS-8 |

## Context

People send WhatsApp messages in fragments: "fiz supino", "3x10", "80kg" are
one thought in three messages. §8 requires them to be grouped by a 3s sliding
window with a 10s absolute cap, and Q113 requires a generation counter so that
a timer scheduled for an older state cannot flush a batch that has since grown.

The spec mandates the *guarantee*. It does not say what fires the flush, and
§43's ADR list therefore does not cover the choice — but §48 requires a
decision like this to be recorded rather than left implicit in code, which is
why this ADR exists outside the numbered list.

## Decision

The flush is triggered by a **delayed RabbitMQ message** carrying the debounce
generation it was scheduled for. Each fragment publishes a trigger into
`debounce.delay`, a queue with no consumer whose messages carry a per-message
expiration; on expiry the broker dead-letters the trigger to `debounce.flush`,
where the aggregator consumes it.

On consumption the generation decides:

- **equal to the current generation** — flush;
- **behind it** — drop, because a newer trigger is already scheduled for the
  fragment that arrived after this one;
- **behind it but past the absolute cap** — flush anyway. Dropping there could
  leave the batch waiting on a trigger that has already been consumed, and
  flushing slightly late is recoverable while never flushing is not;
- **ahead of it** — flush. §10 makes Redis non-authoritative, so a lost and
  rebuilt keyspace restarts the counter below a trigger already in flight.

The expiration is per message rather than per queue because the delay depends
on how much of the absolute window remains: 3s for a fresh window, less for one
about to hit its cap.

## Consequences

**Bought.** The trigger survives a worker restart, a deploy and a crash — an
in-process timer does not, and a window whose timer died would sit in Redis
with nothing left to fire it. No worker sleeps while holding a message, which
§9.3 forbids. The mechanism is the same broker the rest of the system already
depends on, so there is no new moving part.

**Paid.** Per-message TTL in a FIFO queue means a trigger with a short delay
behind one with a longer delay waits for the head to expire. Every delay here
is at most the sliding window, so the wait is bounded by 3s, and the stale-
trigger rule keeps a late trigger harmless. There is also one trigger per
fragment, most of which are discarded as stale — cheap, but visible in broker
metrics as traffic that does nothing.

## Alternatives considered

**An in-process `asyncio` timer.** Rejected: it is the simplest thing that
works until the process restarts, and then the window is orphaned. Recovering
would need a sweeper, which is a second mechanism doing the first one's job.

**RabbitMQ's delayed-message-exchange plugin.** Rejected in D4: it is a plugin,
and managed brokers may not offer it. TTL plus dead-lettering is core AMQP.

**A periodic sweep over open windows.** Rejected: it trades a precise 3s window
for the sweep interval, and either the interval is short — most sweeps finding
nothing — or the debounce becomes noticeably slower than the spec allows.

**Redis keyspace expiry notifications.** Rejected: notifications are fire-and-
forget, so an unavailable consumer drops them silently. That makes Redis
authoritative for something §10 says it must not be.

## How this is enforced

- `tests/unit/test_debounce.py` tests the arithmetic as pure functions before
  Redis or RabbitMQ are involved, including the two staleness exceptions and a
  message arriving at 9s into a 10s cap.
- `tests/integration/test_debounce_batching.py` asserts a stale trigger emits
  nothing, a second flush of one window produces nothing, and a trigger whose
  window is gone still batches what is waiting.
- `tests/e2e/test_walking_skeleton.py` lets the trigger actually expire in the
  delay queue and asserts the reply comes out the other end.
