"""Dead-letter inspection, replay and discard (Q117).

A DLQ nobody can look into is a data loss report written in advance. The three
operations here are the minimum that makes one usable:

* **inspect** -- read messages without consuming them, so looking is safe;
* **replay** -- put them back on their origin queue **with the original
  idempotency key**, which is what makes a replay produce no duplicate business
  effect: the consumer recognises the operation it already processed;
* **discard** -- drop them deliberately, when a replay would only fail again.

Replay resets the attempt counter, because a replayed message is a fresh
decision by an operator rather than a continuation of the failed schedule.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from aio_pika.abc import AbstractChannel

from app.infrastructure.rabbitmq.connection import (
    FAILURE_REASON_HEADER,
    IDEMPOTENCY_HEADER,
    ORIGIN_EXCHANGE_HEADER,
    ORIGIN_QUEUE_HEADER,
    ORIGIN_ROUTING_KEY_HEADER,
    build_raw_message,
)
from app.infrastructure.rabbitmq.topology import dead_letter_queue_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeadLetter:
    idempotency_key: str
    origin_queue: str
    origin_exchange: str
    origin_routing_key: str
    failure_reason: str | None
    attempts: int
    #: Decoded payload when the body is JSON; None when it is not, which is
    #: exactly the case an operator most needs to be able to look at.
    payload: dict[str, Any] | None
    raw_body: bytes
    headers: dict[str, Any]


async def inspect(channel: AbstractChannel, queue: str, *, limit: int = 10) -> list[DeadLetter]:
    """Read up to `limit` dead letters and put them back.

    Requeueing is what makes inspection safe to run in production: looking at
    the queue must not be the thing that empties it.
    """
    dlq = await channel.get_queue(dead_letter_queue_name(queue), ensure=False)
    collected: list[DeadLetter] = []
    held = []

    # The messages are held unacked while the batch is collected. Requeueing
    # each one as it is read would hand the same message back on the next get,
    # and inspection would report one dead letter `limit` times.
    while len(collected) < limit:
        message = await dlq.get(fail=False, no_ack=False)
        if message is None:
            break
        collected.append(_to_dead_letter(message.headers or {}, message.body))
        held.append(message)

    for message in held:
        await message.nack(requeue=True)

    return collected


async def replay(channel: AbstractChannel, queue: str, *, limit: int = 10) -> int:
    """Republish dead letters to their origin queue, keys intact. Returns count."""
    dlq = await channel.get_queue(dead_letter_queue_name(queue), ensure=False)
    replayed = 0

    while replayed < limit:
        message = await dlq.get(fail=False, no_ack=False)
        if message is None:
            break

        dead_letter = _to_dead_letter(message.headers or {}, message.body)
        headers = dict(dead_letter.headers)
        headers.pop(FAILURE_REASON_HEADER, None)

        exchange = await channel.get_exchange(dead_letter.origin_exchange, ensure=False)
        await exchange.publish(
            build_raw_message(
                # Replay is a re-delivery of the same message, so the body goes
                # back exactly as it arrived -- including one that never
                # decoded, which an operator may be replaying after fixing the
                # consumer rather than the payload.
                dead_letter.raw_body,
                # The whole point of Q117: the replayed message carries the key
                # it originally had, so a consumer that already applied it
                # recognises the operation instead of repeating it.
                idempotency_key=dead_letter.idempotency_key,
                correlation_id=message.correlation_id,
                trace_id=str(headers["trace_id"]) if headers.get("trace_id") else None,
                attempts=0,
                headers=headers,
            ),
            routing_key=dead_letter.origin_routing_key,
        )
        await message.ack()
        replayed += 1
        logger.info(
            "dead letter replayed",
            extra={
                "queue": queue,
                "idempotency_key": dead_letter.idempotency_key,
                "origin_routing_key": dead_letter.origin_routing_key,
            },
        )

    return replayed


async def discard(channel: AbstractChannel, queue: str, *, limit: int = 10) -> int:
    """Drop dead letters deliberately. Returns how many were removed."""
    dlq = await channel.get_queue(dead_letter_queue_name(queue), ensure=False)
    discarded = 0

    while discarded < limit:
        message = await dlq.get(fail=False, no_ack=False)
        if message is None:
            break
        await message.ack()
        discarded += 1
        logger.warning(
            "dead letter discarded",
            extra={
                "queue": queue,
                "idempotency_key": str((message.headers or {}).get(IDEMPOTENCY_HEADER)),
            },
        )

    return discarded


def _to_dead_letter(headers: dict[str, Any], body: bytes) -> DeadLetter:
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = None
    payload = decoded if isinstance(decoded, dict) else None

    return DeadLetter(
        idempotency_key=str(headers.get(IDEMPOTENCY_HEADER, "")),
        origin_queue=str(headers.get(ORIGIN_QUEUE_HEADER, "")),
        origin_exchange=str(headers.get(ORIGIN_EXCHANGE_HEADER, "")),
        origin_routing_key=str(headers.get(ORIGIN_ROUTING_KEY_HEADER, "")),
        failure_reason=(
            str(headers[FAILURE_REASON_HEADER]) if headers.get(FAILURE_REASON_HEADER) else None
        ),
        attempts=int(headers.get("x-attempts", 0) or 0),
        payload=payload,
        raw_body=body,
        headers=dict(headers),
    )
