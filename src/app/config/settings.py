"""Typed application settings, validated at startup (§34, Q154).

Two rules shape this module:

* Configuration fails loudly *at startup*. A process that boots with a missing
  password and only discovers it on the first database call has moved a config
  error into production traffic.
* Non-secret configuration and secrets travel separately (§34). Hosts, ports
  and timeouts come from the environment; every secret value comes through the
  :class:`SecretsProvider` port (Q144), which is the single seam a cloud
  secrets manager replaces later.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Any, Final
from urllib.parse import quote

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.application.ports.secrets import SecretsProvider
from app.config.secrets import EnvironmentSecretsProvider

ENV_PREFIX: Final = "GYM_TRACK_"
ENV_NESTED_DELIMITER: Final = "__"
ENV_FILE: Final = ".env"


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ServiceName(StrEnum):
    """Every process that opens its own database connection (Q145).

    `message-aggregator` is on this list because it writes `message_batches`
    and `message_batch_items` in WS-8 -- it needs its own role rather than
    borrowing the API's.
    """

    API = "api"
    MESSAGE_AGGREGATOR = "message-aggregator"
    WORKFLOW_WORKER = "workflow-worker"
    OUTBOX_PUBLISHER = "outbox-publisher"
    DISPATCHER = "dispatcher"
    SESSION_EXPIRATION_WORKER = "session-expiration-worker"


class PostgresRole(BaseModel):
    """One service's database identity. Least privilege starts here (Q145)."""

    user: str = Field(min_length=1)
    password: SecretStr


class PostgresSettings(BaseModel):
    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1)
    roles: dict[ServiceName, PostgresRole]
    # DDL identity. Deliberately not one of the service roles: migrations create
    # and grant those roles, so running them as one of them would defeat Q145.
    admin: PostgresRole

    @model_validator(mode="after")
    def every_service_has_a_role(self) -> PostgresSettings:
        missing = sorted(service.value for service in ServiceName if service not in self.roles)
        if missing:
            raise ValueError(
                "every service that opens a database connection needs its own role (Q145); "
                f"missing: {', '.join(missing)}"
            )
        return self

    def dsn_for(self, service: ServiceName) -> str:
        """Async SQLAlchemy DSN for one service, with its own credentials."""
        return self._dsn(self.roles[service])

    def admin_dsn(self) -> str:
        """Async SQLAlchemy DSN for migrations, which run as the DDL owner."""
        return self._dsn(self.admin)

    def _dsn(self, role: PostgresRole) -> str:
        # Percent-encode the user-info: a generated password containing @, /, ?,
        # # or % would otherwise change where the driver thinks the host starts.
        user = quote(role.user, safe="")
        password = quote(role.password.get_secret_value(), safe="")
        return f"postgresql+asyncpg://{user}:{password}@{self.host}:{self.port}/{self.database}"


class RabbitMQSettings(BaseModel):
    host: str = Field(min_length=1)
    port: int = Field(default=5672, ge=1, le=65535)
    virtual_host: str = "/"
    user: str = Field(min_length=1)
    password: SecretStr
    # Q115: workflow consumers start at prefetch 1 and are revisited only with
    # a benchmark, not with a hunch.
    workflow_prefetch: int = Field(default=1, ge=1)
    # §9.4: delays and max attempts are typed configuration, not constants
    # buried in a consumer. The number of tiers *is* the attempt limit: after
    # the last one a message goes to the DLQ.
    retry_delays: tuple[timedelta, ...] = (
        timedelta(seconds=5),
        timedelta(seconds=30),
        timedelta(minutes=5),
    )

    @model_validator(mode="after")
    def retry_delays_increase(self) -> RabbitMQSettings:
        if not self.retry_delays:
            raise ValueError("at least one retry tier is required before the DLQ")
        if list(self.retry_delays) != sorted(self.retry_delays):
            raise ValueError("retry tiers must increase, otherwise a later retry fires sooner")
        if self.retry_delays[0] <= timedelta(0):
            raise ValueError("retry delays must be positive")
        return self

    @property
    def max_attempts(self) -> int:
        """Deliveries before the DLQ: the first try plus one per retry tier."""
        return len(self.retry_delays) + 1

    def url(self) -> str:
        user = quote(self.user, safe="")
        password = quote(self.password.get_secret_value(), safe="")
        virtual_host = quote(self.virtual_host.lstrip("/"), safe="")
        return f"amqp://{user}:{password}@{self.host}:{self.port}/{virtual_host}"


class RedisSettings(BaseModel):
    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1, le=65535)
    database: int = Field(default=0, ge=0)
    password: SecretStr | None = None

    def url(self) -> str:
        credentials = (
            "" if self.password is None else f":{quote(self.password.get_secret_value(), safe='')}@"
        )
        return f"redis://{credentials}{self.host}:{self.port}/{self.database}"


class WorkflowSettings(BaseModel):
    # DEC-006: 32 partitions is a frozen contract, not a tuning knob -- changing
    # it reorders every user's message stream. It stays configurable only so
    # tests can shrink it deliberately.
    partitions: int = Field(default=32, ge=1)
    debounce_window: timedelta = Field(default=timedelta(seconds=3))
    max_batch_window: timedelta = Field(default=timedelta(seconds=10))
    conversation_timeout: timedelta = Field(default=timedelta(hours=12))
    training_session_timeout: timedelta = Field(default=timedelta(hours=3))
    correction_grace_period: timedelta = Field(default=timedelta(minutes=30))

    @model_validator(mode="after")
    def windows_are_ordered(self) -> WorkflowSettings:
        if self.debounce_window <= timedelta(0):
            raise ValueError("debounce_window must be positive")
        if self.debounce_window >= self.max_batch_window:
            raise ValueError(
                "debounce_window must be shorter than max_batch_window, otherwise the "
                "sliding window can never flush before the absolute cap "
                f"(got {self.debounce_window} >= {self.max_batch_window})"
            )
        return self


class ObservabilitySettings(BaseModel):
    service_name: str = "gym-track"
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")


class SecuritySettings(BaseModel):
    # Verifies the provider's webhook signature before anything is persisted.
    whatsapp_app_secret: SecretStr
    # Q143: the BSUID is stored as ciphertext plus a keyed HMAC used for lookup.
    bsuid_encryption_key: SecretStr
    bsuid_lookup_hmac_key: SecretStr


#: Where each secret lands in the settings tree. Explicit on purpose: a reader
#: can see every secret the process needs, and its name, in one place.
SECRET_BINDINGS: Final[dict[str, tuple[str, ...]]] = {
    **{
        f"postgres/{service.value}/password": ("postgres", "roles", service.value, "password")
        for service in ServiceName
    },
    "postgres/admin/password": ("postgres", "admin", "password"),
    "rabbitmq/password": ("rabbitmq", "password"),
    "redis/password": ("redis", "password"),
    "security/whatsapp-app-secret": ("security", "whatsapp_app_secret"),
    "security/bsuid-encryption-key": ("security", "bsuid_encryption_key"),
    "security/bsuid-lookup-hmac-key": ("security", "bsuid_lookup_hmac_key"),
}


class ApplicationSettings(BaseSettings):
    """The whole configuration surface of every process (§34)."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.LOCAL
    postgres: PostgresSettings
    rabbitmq: RabbitMQSettings
    redis: RedisSettings
    workflow: WorkflowSettings = WorkflowSettings()
    observability: ObservabilitySettings = ObservabilitySettings()
    security: SecuritySettings


def build_secret_tree(provider: SecretsProvider) -> dict[str, Any]:
    """Resolve every declared secret into the shape of the settings tree.

    Absent secrets are simply left out, so pydantic reports them as the missing
    fields they are and names the path an operator has to fix -- which is more
    useful than a provider-level error naming only the secret.
    """
    tree: dict[str, Any] = {}
    for name, path in SECRET_BINDINGS.items():
        value = provider.try_get(name)
        if value is None:
            continue
        branch = tree
        for key in path[:-1]:
            branch = branch.setdefault(key, {})
        branch[path[-1]] = value
    return tree


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def load_settings(
    secrets: SecretsProvider | None = None,
    **overrides: Any,
) -> ApplicationSettings:
    """Build and validate settings, or fail with a legible error.

    This is the composition root for configuration: every entrypoint calls it
    once at startup and passes the result down. Nothing else constructs
    ``ApplicationSettings`` directly.

    Secret values arrive as constructor arguments, which pydantic-settings
    merges *over* the environment. That ordering is deliberate: where a secrets
    manager is in play it is authoritative, and an ambient environment variable
    must not be able to shadow it.
    """
    if secrets is None:
        # The same file the settings themselves are loaded from, unless the
        # caller opted out of dotenv entirely.
        # Whatever pydantic-settings would read, secrets read too: one path, a
        # layered sequence, or nothing when the caller opted out.
        secrets = EnvironmentSecretsProvider(env_file=overrides.get("_env_file", ENV_FILE))
    provider = secrets
    values = _deep_merge(build_secret_tree(provider), overrides)
    return ApplicationSettings(**values)
