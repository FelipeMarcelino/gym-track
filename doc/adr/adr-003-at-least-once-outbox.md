# ADR-003 — At-least-once delivery, transactional outbox and idempotent effects

**Status:** Accepted
**Date:** 2026-08-19

## Traceability

| | |
| --- | --- |
| **Spec sections** | §9.3, §27, §28 |
| **Decision records** | DEC-005 |
| **Interview questions** | Q24-Q25, Q111-Q120, Q130 |
| **Sprint / workstream** | Sprint 1, WS-5, WS-9, WS-10 |

## Context

Three things can each fail independently: the database write, the broker
publish, and the provider call. Any ordering of them has a crash window.

Publishing before committing can announce something that never happened.
Committing before publishing can lose the announcement. Neither is acceptable
for a message a user is waiting on, and "exactly once" across a database and a
broker is not something either system offers.

## Decision

**Delivery is at-least-once, and business effects are idempotent.** The
duplicate is the failure we accept, because it is the one a consumer can
defend against.

Concretely:

1. A domain change, its `domain_events` row and its `outbox_events` row are
   written by the *same session in the same transaction*. There is no window
   where a change exists without its event, or an event without its change.
2. The outbox publisher claims rows with `FOR UPDATE SKIP LOCKED`, publishes,
   waits for the broker's confirmation, and only then marks the row PUBLISHED.
   A crash between the publish and the mark republishes; a crash before the
   publish retries.
3. Consumers deduplicate on stable keys: `event_id` for events,
   `UNIQUE(provider, external_message_id)` for inbound messages,
   `UNIQUE(message_batch_id)` for workflow executions, `UNIQUE(message_id)` for
   batch membership, `UNIQUE(response_group_id, sequence)` for replies.
4. A workflow message is acked after its transaction commits and explicitly
   **not** after the provider delivers (Q130).
5. Provider sends carry an idempotency key stable across restarts, so a crash
   between "the provider accepted" and "the row says so" does not send twice.

## Consequences

**Bought.** No lost events. Every crash window resolves to a duplicate, and
every duplicate resolves to a no-op. Publishers can scale horizontally without
coordinating, because `SKIP LOCKED` makes a second publisher free rather than
dangerous.

**Paid.** Every consumer must be written idempotently, forever — a handler that
forgets is a bug that appears only under redelivery, which is exactly when
nobody is watching. Constraints have to be added *with* each new effect rather
than after it. And a permanently failing row stays PENDING rather than
disappearing, so it needs an alert.

## Alternatives considered

**Publish inside the transaction, to the broker directly.** Rejected: a broker
call inside a database transaction holds the transaction open for a network
round trip, and the publish can still succeed while the commit fails.

**Two-phase commit across PostgreSQL and RabbitMQ.** Rejected: operationally
heavy, poorly supported, and it converts an availability problem into a
liveness problem.

**Exactly-once semantics from the broker.** Rejected as unavailable in the
sense that matters. Deduplication windows are not the same as idempotent
business effects, and relying on them moves correctness into broker
configuration.

## How this is enforced

- `tests/integration/test_outbox.py` asserts a rolled-back transaction leaves
  no event and no outbox row, that two concurrent publishers never claim the
  same row, that a failed publish leaves the row PENDING with a backoff, and
  that a duplicate publication delivers the same `event_id` twice.
- `tests/integration/test_workflow_worker.py` asserts redelivery resumes the
  existing execution and creates no second outbound message.
- `tests/e2e/test_walking_skeleton.py` injects the failures: a redelivered
  workflow message, a triplicate publication, a broker outage.
- `tests/contract/test_domain_event_envelope.py` freezes the wire format the
  deduplication key travels in.
