"""WS-2: the SecretsProvider seam (Q144)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.application.ports.secrets import MissingSecretError, SecretsProvider
from app.config import (
    SECRET_BINDINGS,
    EnvironmentSecretsProvider,
    MappingSecretsProvider,
    build_secret_tree,
    secret_name_to_env_var,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("postgres/api/password", "GYM_TRACK_SECRET_POSTGRES_API_PASSWORD"),
        (
            "postgres/message-aggregator/password",
            "GYM_TRACK_SECRET_POSTGRES_MESSAGE_AGGREGATOR_PASSWORD",
        ),
        ("security/bsuid-encryption-key", "GYM_TRACK_SECRET_SECURITY_BSUID_ENCRYPTION_KEY"),
    ],
)
def test_secret_names_map_to_a_dedicated_env_namespace(name: str, expected: str) -> None:
    assert secret_name_to_env_var(name) == expected


def test_every_declared_secret_maps_to_a_distinct_env_var() -> None:
    env_vars = [secret_name_to_env_var(name) for name in SECRET_BINDINGS]
    assert len(set(env_vars)) == len(env_vars), "two secrets would read the same variable"


def test_environment_provider_reads_from_its_prefix() -> None:
    provider = EnvironmentSecretsProvider({"GYM_TRACK_SECRET_POSTGRES_API_PASSWORD": "s3cret"})

    assert provider.get("postgres/api/password") == SecretStr("s3cret")


def test_environment_provider_treats_an_empty_value_as_absent() -> None:
    provider = EnvironmentSecretsProvider({"GYM_TRACK_SECRET_RABBITMQ_PASSWORD": ""})

    assert provider.try_get("rabbitmq/password") is None


def test_missing_secret_names_itself() -> None:
    provider = EnvironmentSecretsProvider({})

    with pytest.raises(MissingSecretError) as excinfo:
        provider.get("security/bsuid-encryption-key")

    assert "security/bsuid-encryption-key" in str(excinfo.value)
    assert excinfo.value.name == "security/bsuid-encryption-key"


@pytest.mark.parametrize(
    "provider",
    [EnvironmentSecretsProvider({}), MappingSecretsProvider({})],
)
def test_implementations_satisfy_the_port(provider: SecretsProvider) -> None:
    assert isinstance(provider, SecretsProvider)


def test_secret_tree_lands_values_where_the_settings_expect_them() -> None:
    provider = MappingSecretsProvider(
        {
            "postgres/api/password": "pg-api",
            "rabbitmq/password": "rabbit",
            "security/bsuid-lookup-hmac-key": "hmac",
        }
    )

    tree = build_secret_tree(provider)

    assert tree["postgres"]["roles"]["api"]["password"] == SecretStr("pg-api")
    assert tree["rabbitmq"]["password"] == SecretStr("rabbit")
    assert tree["security"]["bsuid_lookup_hmac_key"] == SecretStr("hmac")
    assert "redis" not in tree, "absent secrets must be omitted, not defaulted"
