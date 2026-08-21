"""WS-2: configuration must be complete, ordered and loud when it is not (§34)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from pydantic import ValidationError

from app.config import (
    SECRET_BINDINGS,
    ApplicationSettings,
    Environment,
    MappingSecretsProvider,
    ServiceName,
    load_settings,
    secret_name_to_env_var,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: env var -> logical secret name, the inverse of what the provider does.
_ENV_VAR_TO_SECRET = {secret_name_to_env_var(name): name for name in SECRET_BINDINGS}


def _parse_env_example() -> tuple[dict[str, str], dict[str, str]]:
    """Split the committed example into (non-secret env, secret values).

    Tests read the example rather than restating it, which means an option added
    to the code but forgotten in `.env.example` fails here instead of failing on
    a teammate's first run.
    """
    environment: dict[str, str] = {}
    secrets: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        secret_name = _ENV_VAR_TO_SECRET.get(key)
        if secret_name is not None:
            secrets[secret_name] = value
        else:
            environment[key] = value
    return environment, secrets


EXAMPLE_ENV, EXAMPLE_SECRETS = _parse_env_example()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The environment described by `.env.example`, isolated from the real one."""
    for key in list(EXAMPLE_ENV) + [secret_name_to_env_var(n) for n in SECRET_BINDINGS]:
        monkeypatch.delenv(key, raising=False)
    for key, value in EXAMPLE_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(EXAMPLE_ENV)


def _load(**overrides: Any) -> ApplicationSettings:
    # _env_file=None keeps a developer's real .env out of the test run.
    return load_settings(MappingSecretsProvider(EXAMPLE_SECRETS), _env_file=None, **overrides)


def test_env_example_covers_every_declared_secret() -> None:
    missing = sorted(set(SECRET_BINDINGS) - set(EXAMPLE_SECRETS) - {"redis/password"})
    assert not missing, f".env.example does not document: {missing}"


def test_valid_environment_produces_fully_populated_settings(env: dict[str, str]) -> None:
    settings = _load()

    assert settings.environment is Environment.LOCAL
    assert settings.postgres.host == "localhost"
    assert settings.postgres.database == "gym_track"
    assert settings.rabbitmq.user == "gym_track"
    assert settings.rabbitmq.workflow_prefetch == 1
    assert settings.redis.database == 0
    assert settings.workflow.partitions == 32
    assert settings.workflow.debounce_window == timedelta(seconds=3)
    assert settings.workflow.max_batch_window == timedelta(seconds=10)
    assert settings.observability.log_level == "INFO"


def test_every_service_carries_its_own_database_role(env: dict[str, str]) -> None:
    settings = _load()

    users = {service: settings.postgres.roles[service].user for service in ServiceName}
    assert set(users) == set(ServiceName), "a process without a role would borrow another's (Q145)"
    assert len(set(users.values())) == len(ServiceName), "roles must not be shared between services"


def test_missing_service_role_is_rejected_by_name(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping a service's role must fail loudly, not silently share another's."""
    monkeypatch.delenv("GYM_TRACK_POSTGRES__ROLES__DISPATCHER__USER")
    without_dispatcher = {
        name: value
        for name, value in EXAMPLE_SECRETS.items()
        if name != "postgres/dispatcher/password"
    }

    with pytest.raises(ValidationError) as excinfo:
        load_settings(MappingSecretsProvider(without_dispatcher), _env_file=None)

    message = str(excinfo.value)
    assert "dispatcher" in message
    assert "Q145" in message


def test_missing_required_value_fails_at_startup_and_says_which(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GYM_TRACK_POSTGRES__HOST")

    with pytest.raises(ValidationError) as excinfo:
        _load()

    message = str(excinfo.value)
    assert "postgres.host" in message
    assert "Field required" in message


def test_missing_secret_is_reported_as_the_field_it_feeds(env: dict[str, str]) -> None:
    without_app_secret = {
        name: value
        for name, value in EXAMPLE_SECRETS.items()
        if name != "security/whatsapp-app-secret"
    }

    with pytest.raises(ValidationError) as excinfo:
        load_settings(MappingSecretsProvider(without_app_secret), _env_file=None)

    assert "security.whatsapp_app_secret" in str(excinfo.value)


@pytest.mark.parametrize(
    ("workflow_overrides", "expected"),
    [
        ({"partitions": 0}, "greater than or equal to 1"),
        ({"debounce_window": "PT0S"}, "debounce_window must be positive"),
        ({"debounce_window": "PT10S", "max_batch_window": "PT10S"}, "shorter than"),
        ({"debounce_window": "PT30S", "max_batch_window": "PT10S"}, "shorter than"),
        # A question that expires the instant it is asked can never be
        # answered, and the failure would look like a user who never replied.
        ({"clarification_timeout": "PT0S"}, "clarification_timeout must be positive"),
        ({"clarification_timeout": "-PT1H"}, "clarification_timeout must be positive"),
    ],
)
def test_out_of_range_workflow_values_are_rejected(
    env: dict[str, str], workflow_overrides: dict[str, Any], expected: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _load(workflow=workflow_overrides)

    assert expected in str(excinfo.value)


def test_invalid_log_level_is_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        _load(observability={"log_level": "CHATTY"})


def test_secrets_never_appear_in_repr_or_serialization(env: dict[str, str]) -> None:
    settings = _load()
    secret_values = set(EXAMPLE_SECRETS.values())

    renderings = [
        repr(settings),
        str(settings),
        json.dumps(settings.model_dump(mode="json")),
        settings.model_dump_json(),
    ]

    for rendering in renderings:
        for value in secret_values:
            assert value not in rendering, "a secret leaked into a rendering of the settings"
    assert "**********" in repr(settings)


def test_secret_values_are_reachable_only_through_explicit_accessors(
    env: dict[str, str],
) -> None:
    settings = _load()

    dsn = settings.postgres.dsn_for(ServiceName.WORKFLOW_WORKER)
    assert dsn.startswith("postgresql+asyncpg://gym_workflow_worker:")
    assert EXAMPLE_SECRETS["postgres/workflow-worker/password"] in dsn
    assert "@localhost:5432/gym_track" in dsn

    assert settings.rabbitmq.url().startswith("amqp://gym_track:")
    assert settings.redis.url() == "redis://localhost:6379/0"


def test_provider_secret_outranks_an_ambient_environment_variable(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secrets manager is authoritative where it is in play (§34)."""
    monkeypatch.setenv("GYM_TRACK_RABBITMQ__PASSWORD", "from-ambient-env")

    settings = _load()

    assert settings.rabbitmq.password.get_secret_value() == EXAMPLE_SECRETS["rabbitmq/password"]


def test_settings_are_frozen(env: dict[str, str]) -> None:
    settings = _load()

    with pytest.raises(ValidationError):
        settings.environment = Environment.PRODUCTION


@pytest.mark.parametrize(
    "password",
    [
        "p@ss/word",
        "with#hash?query",
        "percent%20encoded",
        "dollar$$quoted",
        "quote'and\"quote",
    ],
)
def test_dsn_survives_passwords_containing_url_delimiters(
    env: dict[str, str], password: str
) -> None:
    """A generated password routinely contains @ or /, and interpolating it raw
    moves where the driver thinks the host begins."""
    secrets = dict(EXAMPLE_SECRETS)
    secrets["postgres/api/password"] = password
    secrets["rabbitmq/password"] = password
    settings = load_settings(MappingSecretsProvider(secrets), _env_file=None)

    dsn = settings.postgres.dsn_for(ServiceName.API)
    parsed = urlsplit(dsn)

    assert parsed.hostname == "localhost"
    assert parsed.port == 5432
    assert parsed.path == "/gym_track"
    assert parsed.password is not None
    assert unquote(parsed.password) == password
    assert urlsplit(settings.rabbitmq.url()).hostname == "localhost"


def test_a_dotenv_copied_from_the_example_is_enough_to_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `.env.example` promise, taken literally.

    Settings were loaded from `.env` while secrets were read only from the
    process environment, so a `.env` copied from the committed example
    populated every non-secret value and silently none of the secrets — and a
    clean clone could not run `make migrate`.
    """
    for key in list(EXAMPLE_ENV) + [secret_name_to_env_var(n) for n in SECRET_BINDINGS]:
        monkeypatch.delenv(key, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    settings = load_settings(_env_file=str(env_file))

    assert settings.postgres.host == "localhost"
    assert settings.security.whatsapp_app_secret.get_secret_value() == "local-dev-only"
    assert settings.postgres.roles[ServiceName.API].password.get_secret_value() == "local-dev-only"


def test_an_exported_secret_wins_over_the_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment exports its secrets; a stale file must not shadow them."""
    for key in list(EXAMPLE_ENV) + [secret_name_to_env_var(n) for n in SECRET_BINDINGS]:
        monkeypatch.delenv(key, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("GYM_TRACK_SECRET_SECURITY_WHATSAPP_APP_SECRET", "from-the-environment")

    settings = load_settings(_env_file=str(env_file))

    assert settings.security.whatsapp_app_secret.get_secret_value() == "from-the-environment"


def test_layered_dotenv_files_override_in_pydantic_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_env_file` may be a sequence, and the later file wins — for secrets too,
    or the two halves of one configuration disagree about which file is
    authoritative."""
    for key in list(EXAMPLE_ENV) + [secret_name_to_env_var(n) for n in SECRET_BINDINGS]:
        monkeypatch.delenv(key, raising=False)

    base = tmp_path / ".env"
    base.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    override = tmp_path / ".env.production"
    override.write_text(
        "GYM_TRACK_POSTGRES__HOST=db.internal\n"
        "GYM_TRACK_SECRET_SECURITY_WHATSAPP_APP_SECRET=from-the-later-file\n",
        encoding="utf-8",
    )

    settings = load_settings(_env_file=(str(base), str(override)))

    assert settings.postgres.host == "db.internal"
    assert settings.security.whatsapp_app_secret.get_secret_value() == "from-the-later-file"
    assert settings.rabbitmq.password.get_secret_value() == "local-dev-only", (
        "values the later file does not mention still come from the base file"
    )


def test_a_missing_dotenv_file_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deployments export their configuration and ship no file at all."""
    for key, value in EXAMPLE_ENV.items():
        monkeypatch.setenv(key, value)
    for name, value in EXAMPLE_SECRETS.items():
        monkeypatch.setenv(secret_name_to_env_var(name), value)

    settings = load_settings(_env_file=str(tmp_path / "absent.env"))

    assert settings.postgres.host == "localhost"
