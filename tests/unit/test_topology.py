"""WS-6: the topology is data, so it can be asserted without a broker (§9)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import SecretStr, ValidationError

from app.config import RabbitMQSettings
from app.infrastructure.rabbitmq.retry import RetryPolicy
from app.infrastructure.rabbitmq.topology import (
    Exchanges,
    build_topology,
    dead_letter_queue_name,
    retry_queue_name,
)

BUSINESS_QUEUES = ("message.received", "outbound.dispatch", "background.jobs")


@pytest.fixture
def settings() -> RabbitMQSettings:
    return RabbitMQSettings(host="broker", user="gym_track", password=SecretStr("secret"))


def test_the_five_exchanges_of_section_9_1_exist(settings: RabbitMQSettings) -> None:
    topology = build_topology(settings)
    names = {exchange.name for exchange in topology.exchanges}

    assert {
        Exchanges.WHATSAPP_INBOUND,
        Exchanges.WORKFLOW,
        Exchanges.DOMAIN_EVENTS,
        Exchanges.BACKGROUND,
        Exchanges.WHATSAPP_OUTBOUND,
    } <= names


def test_there_are_thirty_two_workflow_partitions(settings: RabbitMQSettings) -> None:
    topology = build_topology(settings)
    partitions = [queue for queue in topology.queues if queue.name.startswith("workflow.0")]

    assert topology.queue("workflow.00")
    assert topology.queue("workflow.31")
    assert len(partitions) >= 10


def test_every_partition_queue_has_single_active_consumer(settings: RabbitMQSettings) -> None:
    """Q114: ordering per user is infrastructure behaviour, not a lock."""
    topology = build_topology(settings)

    for partition in range(32):
        queue = topology.queue(f"workflow.{partition:02d}")
        assert queue.single_active_consumer
        assert queue.declaration_arguments()["x-single-active-consumer"] is True


def test_retry_tiers_carry_the_configured_delays(settings: RabbitMQSettings) -> None:
    topology = build_topology(settings)

    for tier, expected_ms in ((1, 5_000), (2, 30_000), (3, 300_000)):
        queue = topology.queue(retry_queue_name("workflow.00", tier))
        assert queue.arguments["x-message-ttl"] == expected_ms
        assert queue.arguments["x-dead-letter-routing-key"] == "workflow.00"
        assert queue.arguments["x-dead-letter-exchange"] == Exchanges.RETRY


def test_an_expired_retry_message_can_route_back_to_its_origin(
    settings: RabbitMQSettings,
) -> None:
    """The binding that closes the loop: without it, a message that finished its
    delay would be dead-lettered into an exchange nothing is listening on."""
    topology = build_topology(settings)

    for name in ("workflow.00", *BUSINESS_QUEUES):
        bindings = {
            (binding.exchange, binding.routing_key) for binding in topology.queue(name).bindings
        }
        assert (Exchanges.RETRY, name) in bindings


@pytest.mark.parametrize("queue", ["workflow.00", "workflow.31", *BUSINESS_QUEUES])
def test_every_business_queue_has_retry_tiers_and_a_dlq(
    settings: RabbitMQSettings, queue: str
) -> None:
    topology = build_topology(settings)

    for tier in (1, 2, 3):
        assert topology.queue(retry_queue_name(queue, tier))
    assert topology.queue(dead_letter_queue_name(queue))


def test_retry_queues_have_no_single_active_consumer(settings: RabbitMQSettings) -> None:
    """They have no consumer at all: they are timers made of queue arguments."""
    topology = build_topology(settings)
    retry_queue = topology.queue(retry_queue_name("workflow.00", 1))

    assert not retry_queue.single_active_consumer
    assert "x-single-active-consumer" not in retry_queue.declaration_arguments()


def test_dead_letter_queues_do_not_expire_their_contents(settings: RabbitMQSettings) -> None:
    """A DLQ with a TTL is a delete button with a delay."""
    topology = build_topology(settings)

    assert topology.queue(dead_letter_queue_name("workflow.00")).arguments == {}


def test_topology_is_deterministic(settings: RabbitMQSettings) -> None:
    assert build_topology(settings) == build_topology(settings)


def test_queue_names_are_unique(settings: RabbitMQSettings) -> None:
    names = [queue.name for queue in build_topology(settings).queues]

    assert len(names) == len(set(names))


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------


def test_the_policy_walks_the_tiers_then_hands_over_to_the_dlq() -> None:
    policy = RetryPolicy((timedelta(seconds=5), timedelta(seconds=30), timedelta(minutes=5)))

    assert policy.next_tier(0) == 1
    assert policy.next_tier(1) == 2
    assert policy.next_tier(2) == 3
    assert policy.next_tier(3) is None, "after the last tier comes the DLQ"
    assert policy.delay_for(1) == timedelta(seconds=5)
    assert policy.delay_for(3) == timedelta(minutes=5)


def test_a_policy_without_tiers_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one retry tier"):
        RetryPolicy(())


def test_retry_delays_must_increase() -> None:
    with pytest.raises(ValidationError, match="must increase"):
        RabbitMQSettings(
            host="broker",
            user="u",
            password=SecretStr("p"),
            retry_delays=(timedelta(seconds=30), timedelta(seconds=5)),
        )


def test_max_attempts_counts_the_first_delivery(settings: RabbitMQSettings) -> None:
    assert settings.max_attempts == 4, "one attempt plus three retries"
