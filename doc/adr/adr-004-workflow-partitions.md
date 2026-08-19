# ADR-004 — 32 stable-hash workflow partitions with Single Active Consumer

**Status:** Accepted
**Date:** 2026-08-19

## Traceability

| | |
| --- | --- |
| **Spec sections** | §9.2, §9.3, §37.3 |
| **Decision records** | DEC-006 |
| **Interview questions** | Q23, Q114-Q115 |
| **Sprint / workstream** | Sprint 1, WS-6 |

## Context

Two messages from the same user must not be processed concurrently: they share
a conversation, a training session and eventually a LangGraph thread, and
interleaving them produces a reply that answers the wrong half of what was
said. Messages from *different* users have no such relationship and should run
in parallel.

The usual instinct is a distributed lock per user. That makes ordering an
application concern, and every lock has a lease, a renewal and a failure mode
that looks like a hang.

## Decision

Ordering is infrastructure behaviour. A user's messages always land on the same
queue, and that queue has one consumer at a time:

```
partition = blake2b(user_id.bytes) % 32
queues:    workflow.00 .. workflow.31
```

- **The hash is a contract, not an implementation detail.** Python's built-in
  `hash()` is forbidden by §9.2 because it is salted per process: two workers of
  one deployment would disagree about where a user belongs. blake2b over the
  UUID's 16 raw bytes is stable across processes, machines and Python versions.
- **Single Active Consumer** per partition queue: several workers may subscribe,
  only one receives.
- **Prefetch 1** to start (Q115). SAC picks one consumer; it says nothing about
  how many unacknowledged deliveries that consumer holds, and an async handler
  with a prefetch of 50 processes a partition's backlog concurrently — losing
  exactly the ordering the partition exists to provide.
- **32 partitions**, revisited only with a benchmark.

## Consequences

**Bought.** Per-user ordering with no lock, no lease and no renewal. Parallelism
across users bounded by a number that is easy to reason about. A stuck user
blocks one partition rather than the system.

**Paid.** Throughput per user is one message at a time, by design. A slow
handler blocks its partition, and the users hashing to it wait. Changing the
partition count reshuffles every existing user's stream and can interleave two
executions for one conversation mid-flight, so it is not a runtime knob — it is
a migration.

## Alternatives considered

**A distributed lock per user (Redis).** Rejected: it moves an ordering
guarantee into application code and adds a failure mode — a lost lease under
load — that presents as duplicate concurrent processing, which is the exact
thing being prevented.

**One queue per user.** Rejected: unbounded queue count, and per-queue overhead
that grows with a metric the product wants to grow.

**Consistent hashing with a variable ring.** Rejected as premature. It buys
resharding without reordering, at the cost of complexity nothing yet needs; 32
fixed partitions can be revisited when a benchmark says so.

## How this is enforced

- `tests/unit/test_partitioning.py` freezes eight user-id → partition pairs as
  golden values. A change to the hash fails there, and the correct response is
  to revert the change rather than update the expectations.
- The same file recomputes a partition in subprocesses under three different
  `PYTHONHASHSEED` values, which is what would have caught `hash()`.
- `tests/unit/test_topology.py` asserts every partition queue declares
  `x-single-active-consumer`.
- `tests/integration/test_rabbitmq.py` runs two consumers against one partition
  and asserts only one is ever active, and that a consumer channel opened with
  prefetch 1 does not receive a second message while the first is unacked.
