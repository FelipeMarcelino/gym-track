"""WS-13: the parts of process startup that can be checked without containers."""

from __future__ import annotations

import pytest

from app.config import ApplicationSettings, Environment, MappingSecretsProvider, load_settings
from app.entrypoints.dispatcher import build_client
from app.entrypoints.workflow_worker import _owned_partitions
from app.infrastructure.whatsapp.fake_client import FakeWhatsAppClient
from tests.unit.test_settings import EXAMPLE_SECRETS


def settings_for(environment: Environment) -> ApplicationSettings:
    return load_settings(
        MappingSecretsProvider(EXAMPLE_SECRETS),
        _env_file=None,
        environment=environment,
        postgres={
            "host": "localhost",
            "database": "gym_track",
            "admin": {"user": "gym_track"},
            "roles": {
                "api": {"user": "gym_api"},
                "message-aggregator": {"user": "gym_message_aggregator"},
                "workflow-worker": {"user": "gym_workflow_worker"},
                "outbox-publisher": {"user": "gym_outbox_publisher"},
                "dispatcher": {"user": "gym_dispatcher"},
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
