"""WS-13: the parts of process startup that can be checked without containers."""

from __future__ import annotations

import pytest

from app.config import (
    ApplicationSettings,
    Environment,
    MappingSecretsProvider,
    ServiceName,
    load_settings,
)
from app.entrypoints.dispatcher import build_client
from app.entrypoints.workflow_worker import _owned_partitions
from app.infrastructure.whatsapp.fake_client import FakeWhatsAppClient
from tests.unit.test_settings import EXAMPLE_SECRETS


def settings_for(environment: Environment) -> ApplicationSettings:
    """Settings built from nothing but this file.

    The roles are derived from `ServiceName` rather than listed. A literal list
    silently goes stale the moment a sprint adds a process -- which is exactly
    what happened when the session-expiration worker arrived, and it was hidden
    because another suite's session fixture had already exported the missing
    variable into the environment. Settings assembled from a source of truth
    cannot drift that way.
    """
    return load_settings(
        MappingSecretsProvider(EXAMPLE_SECRETS),
        _env_file=None,
        environment=environment,
        postgres={
            "host": "localhost",
            "database": "gym_track",
            "admin": {"user": "gym_track"},
            "roles": {
                service.value: {"user": f"gym_{service.value.replace('-', '_')}"}
                for service in ServiceName
            },
        },
        rabbitmq={"host": "rabbitmq", "user": "gym_track"},
        redis={"host": "redis"},
    )


@pytest.mark.parametrize("environment", [Environment.LOCAL, Environment.TEST])
def test_the_fake_client_is_used_where_there_is_no_real_integration(
    environment: Environment,
) -> None:
    assert isinstance(build_client(settings_for(environment)), FakeWhatsAppClient)


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_a_deployed_dispatcher_refuses_to_start_without_a_real_client(
    environment: Environment,
) -> None:
    """Falling back to the fake client outside local would drop every reply
    while reporting success — the process failing to start is the honest
    outcome until the Meta integration exists (D6)."""
    with pytest.raises(NotImplementedError, match="real integration"):
        build_client(settings_for(environment))


def test_a_worker_subscribes_to_every_partition_by_default() -> None:
    """Single Active Consumer decides who gets each one, which is what survives
    a replica dying."""
    assert _owned_partitions(32) == list(range(32))


def test_a_worker_can_be_pinned_to_a_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GYM_TRACK_WORKFLOW_PARTITIONS_OWNED", "0,3, 7")

    assert _owned_partitions(32) == [0, 3, 7]


def test_an_empty_pin_falls_back_to_every_partition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GYM_TRACK_WORKFLOW_PARTITIONS_OWNED", "")

    assert _owned_partitions(4) == [0, 1, 2, 3]


# --------------------------------------------------------------------------
# Graceful shutdown
# --------------------------------------------------------------------------


async def test_one_stop_event_is_shared_by_every_consumer() -> None:
    """A workflow worker runs up to 32 consumers. Installing a signal handler
    per consumer means each installation replaces the last, so SIGTERM would
    wake only one of them and the process would have to be killed."""
    from app.entrypoints.runtime import shutdown_event

    first = shutdown_event()
    second = shutdown_event()

    assert first is second

    first.set()
    assert second.is_set()


async def test_every_waiter_wakes_on_the_stop_event() -> None:
    import asyncio

    from app.entrypoints.runtime import shutdown_event

    stop = shutdown_event()
    stop.clear()
    waiters = [asyncio.create_task(stop.wait()) for _ in range(5)]
    await asyncio.sleep(0)

    stop.set()
    await asyncio.wait_for(asyncio.gather(*waiters), timeout=1)

    assert all(task.done() for task in waiters)
    stop.clear()


async def test_the_outbox_loop_stops_when_asked() -> None:
    """SIGTERM during a shutdown must end the loop rather than let the process
    be killed with a connection open."""
    import asyncio

    from app.entrypoints.outbox_publisher import publish_until_stopped
    from app.workers.outbox_publisher import PublishResult

    passes = 0

    class CountingPublisher:
        async def publish_pending(self) -> PublishResult:
            nonlocal passes
            passes += 1
            return PublishResult(published=0, failed=0)

    stop = asyncio.Event()
    loop = asyncio.create_task(
        publish_until_stopped(CountingPublisher(), stop, idle_interval=0.01)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.05)
    stop.set()

    await asyncio.wait_for(loop, timeout=1)
    assert passes >= 1


async def test_the_outbox_loop_keeps_draining_while_there_is_work() -> None:
    """A burst must not be paced by the idle interval."""
    import asyncio

    from app.entrypoints.outbox_publisher import publish_until_stopped
    from app.workers.outbox_publisher import PublishResult

    remaining = 5

    class BurstPublisher:
        async def publish_pending(self) -> PublishResult:
            nonlocal remaining
            if remaining:
                remaining -= 1
                return PublishResult(published=10, failed=0)
            return PublishResult(published=0, failed=0)

    stop = asyncio.Event()
    task = asyncio.create_task(
        publish_until_stopped(BurstPublisher(), stop, idle_interval=5)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.05)

    assert remaining == 0, "the burst drained without waiting out the idle interval"
    stop.set()
    await asyncio.wait_for(task, timeout=1)
