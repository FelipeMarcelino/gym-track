"""WS-6 against a real broker: topology, retry traversal, SAC and DLQ replay."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from pydantic import SecretStr

from app.config import RabbitMQSettings
from app.infrastructure.rabbitmq import dlq
from app.infrastructure.rabbitmq.connection import (
    IDEMPOTENCY_HEADER,
    build_message,
    connect,
    declare_topology,
)
from app.infrastructure.rabbitmq.retry import RetryPolicy, attempts_of, handle_with_retry
from app.infrastructure.rabbitmq.topology import (
    Exchanges,
    build_topology,
    dead_letter_queue_name,
    retry_queue_name,
)

pytestmark = [pytest.mark.integration]

# Short enough to watch a message traverse every tier inside a test, while the
# production schedule stays 5s/30s/5m.
FAST_TIERS = (timedelta(milliseconds=200), timedelta(milliseconds=200), timedelta(milliseconds=200))
QUEUE = "workflow.00"


@pytest.fixture
def settings() -> RabbitMQSettings:
    return RabbitMQSettings(
        host="unused",
        user="gym_track",
        password=SecretStr("integration-test"),
        retry_delays=FAST_TIERS,
    )


@pytest.fixture
async def channel(rabbitmq_url: str, settings: RabbitMQSettings) -> AsyncIterator[AbstractChannel]:
    connection = await connect(rabbitmq_url)
    try:
        async with connection:
            open_channel = await connection.channel(publisher_confirms=True)
            await declare_topology(open_channel, build_topology(settings, partitions=2))
            await _drain(open_channel)
            yield open_channel
    finally:
        await connection.close()


async def _drain(channel: AbstractChannel) -> None:
    """Leave no message behind for the next test, without dropping the topology."""
    for name in (
        QUEUE,
        dead_letter_queue_name(QUEUE),
        *(retry_queue_name(QUEUE, tier) for tier in (1, 2, 3)),
    ):
        queue = await channel.get_queue(name, ensure=False)
        await queue.purge()


async def _publish(channel: AbstractChannel, *, key: str, body: dict[str, object]) -> None:
    exchange = await channel.get_exchange(Exchanges.WORKFLOW, ensure=False)
    await exchange.publish(build_message(body, idempotency_key=key), routing_key=QUEUE)


async def _consume_one(
    channel: AbstractChannel,
    handler: object,
    *,
    policy: RetryPolicy,
    timeout: float = 5.0,
) -> bool | None:
    """Take one message from the queue and run it through the retry wrapper."""
    queue = await channel.get_queue(QUEUE, ensure=False)
    deadline = asyncio.get_running_loop().time() + timeout

    while asyncio.get_running_loop().time() < deadline:
        message = await queue.get(fail=False)
        if message is not None:
            return await handle_with_retry(
                message,
                handler,  # type: ignore[arg-type]
                channel=channel,
                queue=QUEUE,
                policy=policy,
            )
        await asyncio.sleep(0.05)
    return None


async def _peek(channel: AbstractChannel, name: str) -> dict[str, Any] | None:
    """Look at the first message of a queue and leave it there.

    A passive declare's `message_count` is not trustworthy here -- it reports
    zero for messages that a `get` immediately returns -- so the queue is read
    instead of counted.
    """
    queue = await channel.get_queue(name, ensure=False)
    message = await queue.get(fail=False)
    if message is None:
        return None
    headers: dict[str, Any] = dict(message.headers or {})
    await message.nack(requeue=True)
    return headers


async def test_declaring_the_topology_twice_is_a_no_op(
    channel: AbstractChannel, settings: RabbitMQSettings
) -> None:
    """Every worker declares at start, so re-declaration must be safe."""
    topology = build_topology(settings, partitions=2)

    await declare_topology(channel, topology)
    await declare_topology(channel, topology)

    queue = await channel.get_queue(QUEUE, ensure=False)
    assert queue.name == QUEUE


async def test_a_message_failing_repeatedly_walks_every_tier_then_lands_in_the_dlq(
    channel: AbstractChannel,
) -> None:
    """§9.4's schedule, observed end to end: 3 tiers, then the DLQ."""
    policy = RetryPolicy(FAST_TIERS)
    seen_attempts: list[int] = []

    async def always_fails(message: AbstractIncomingMessage) -> None:
        seen_attempts.append(attempts_of(message))
        raise RuntimeError("handler exploded")

    await _publish(channel, key="op-1", body={"value": 1})

    for _ in range(4):
        assert await _consume_one(channel, always_fails, policy=policy) is False

    assert seen_attempts == [0, 1, 2, 3], "each redelivery carries an incremented attempt count"

    dead = await dlq.inspect(channel, QUEUE)
    assert len(dead) == 1
    assert dead[0].idempotency_key == "op-1"
    assert dead[0].origin_queue == QUEUE
    assert dead[0].failure_reason is not None and "handler exploded" in dead[0].failure_reason


async def test_a_retried_message_waits_in_a_queue_and_comes_back_on_its_own(
    channel: AbstractChannel,
) -> None:
    """Q116: the delay is a queue TTL, so no worker sleeps holding the message.

    Right after the failure the message is in neither the worker nor the origin
    queue -- it is parked in the tier queue. Nothing consumes that queue, and
    the message still reappears on the origin queue once the TTL elapses.
    """
    policy = RetryPolicy(FAST_TIERS)

    async def always_fails(message: AbstractIncomingMessage) -> None:
        raise RuntimeError("nope")

    await _publish(channel, key="op-2", body={"value": 2})
    assert await _consume_one(channel, always_fails, policy=policy) is False

    assert await _peek(channel, QUEUE) is None, "the message is parked, not requeued"

    await asyncio.sleep(0.6)  # comfortably past the 200ms tier TTL

    returned = await _peek(channel, QUEUE)
    assert returned is not None, "the tier queue returned it unaided"
    assert returned["x-first-death-queue"] == retry_queue_name(QUEUE, 1), (
        "it came back through the delay queue, not through a requeue"
    )
    assert returned["x-first-death-reason"] == "expired"


async def test_a_successful_handler_acks_and_leaves_nothing_behind(
    channel: AbstractChannel,
) -> None:
    handled: list[str] = []

    async def succeeds(message: AbstractIncomingMessage) -> None:
        handled.append(json.loads(message.body)["value"])

    await _publish(channel, key="op-3", body={"value": "done"})
    assert await _consume_one(channel, succeeds, policy=RetryPolicy(FAST_TIERS)) is True

    assert handled == ["done"]
    assert await dlq.inspect(channel, QUEUE) == []


async def test_a_replayed_dead_letter_keeps_its_idempotency_key(
    channel: AbstractChannel,
) -> None:
    """Q117, and the reason replay is safe: the consumer sees the same operation
    id it already processed, so it recognises the work instead of repeating it."""
    policy = RetryPolicy((timedelta(milliseconds=100),))
    operation_id = f"op-{uuid4()}"

    async def always_fails(message: AbstractIncomingMessage) -> None:
        raise RuntimeError("handler exploded")

    await _publish(channel, key=operation_id, body={"value": 4})
    for _ in range(2):
        await _consume_one(channel, always_fails, policy=policy)

    assert await dlq.replay(channel, QUEUE) == 1

    replayed_keys: list[str] = []

    async def records(message: AbstractIncomingMessage) -> None:
        replayed_keys.append(str((message.headers or {})[IDEMPOTENCY_HEADER]))

    assert await _consume_one(channel, records, policy=policy) is True
    assert replayed_keys == [operation_id]
    assert await dlq.inspect(channel, QUEUE) == [], "the replayed message left the DLQ"


async def test_inspecting_the_dlq_does_not_consume_it(channel: AbstractChannel) -> None:
    policy = RetryPolicy((timedelta(milliseconds=100),))

    async def always_fails(message: AbstractIncomingMessage) -> None:
        raise RuntimeError("boom")

    await _publish(channel, key="op-5", body={"value": 5})
    for _ in range(2):
        await _consume_one(channel, always_fails, policy=policy)

    first = await dlq.inspect(channel, QUEUE)
    second = await dlq.inspect(channel, QUEUE)

    assert len(first) == len(second) == 1
    assert first[0].idempotency_key == second[0].idempotency_key


async def test_discarding_removes_the_dead_letter(channel: AbstractChannel) -> None:
    policy = RetryPolicy((timedelta(milliseconds=100),))

    async def always_fails(message: AbstractIncomingMessage) -> None:
        raise RuntimeError("boom")

    await _publish(channel, key="op-6", body={"value": 6})
    for _ in range(2):
        await _consume_one(channel, always_fails, policy=policy)

    assert await dlq.discard(channel, QUEUE) == 1
    assert await dlq.inspect(channel, QUEUE) == []


async def test_single_active_consumer_serializes_two_messages_for_one_user(
    channel: AbstractChannel, rabbitmq_url: str
) -> None:
    """Q114: with two consumers on the partition, only one is ever active, so a
    user's messages cannot be processed concurrently."""
    started = 0
    concurrent_peak = 0
    processed: list[str] = []
    done = asyncio.Event()

    async def slow_handler(message: AbstractIncomingMessage) -> None:
        nonlocal started, concurrent_peak
        started += 1
        concurrent_peak = max(concurrent_peak, started)
        await asyncio.sleep(0.2)
        processed.append(json.loads(message.body)["value"])
        started -= 1
        await message.ack()
        if len(processed) == 2:
            done.set()

    queue = await channel.get_queue(QUEUE, ensure=False)
    await channel.set_qos(prefetch_count=1)

    second_connection = await connect(rabbitmq_url)
    try:
        second_channel = await second_connection.channel()
        await second_channel.set_qos(prefetch_count=1)
        rival = await second_channel.get_queue(QUEUE, ensure=False)

        await queue.consume(slow_handler, no_ack=False)
        await rival.consume(slow_handler, no_ack=False)

        await _publish(channel, key="op-7a", body={"value": "first"})
        await _publish(channel, key="op-7b", body={"value": "second"})

        await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        await second_connection.close()

    assert sorted(processed) == ["first", "second"]
    assert concurrent_peak == 1, "single active consumer must serialize the partition"
