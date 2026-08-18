"""Secrets provider implementations (Q144).

The environment-backed provider is what local development and CI use. Anything
production-grade replaces this one object and nothing else.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from pydantic import SecretStr

from app.application.ports.secrets import MissingSecretError

SECRET_ENV_PREFIX = "GYM_TRACK_SECRET_"

_NON_ALPHANUMERIC = re.compile(r"[^A-Za-z0-9]+")


def secret_name_to_env_var(name: str) -> str:
    """``postgres/api/password`` -> ``GYM_TRACK_SECRET_POSTGRES_API_PASSWORD``."""
    return SECRET_ENV_PREFIX + _NON_ALPHANUMERIC.sub("_", name).upper().strip("_")


class EnvironmentSecretsProvider:
    """Reads secrets from the process environment under a dedicated prefix.

    The prefix keeps secrets in their own namespace: settings read
    ``GYM_TRACK_*`` and secrets read ``GYM_TRACK_SECRET_*``, so a value can
    never drift from one category into the other by accident.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def get(self, name: str) -> SecretStr:
        value = self.try_get(name)
        if value is None:
            raise MissingSecretError(name)
        return value

    def try_get(self, name: str) -> SecretStr | None:
        raw = self._environ.get(secret_name_to_env_var(name))
        if raw is None or raw == "":
            return None
        return SecretStr(raw)


class MappingSecretsProvider:
    """In-memory provider, for tests and for composing a process by hand."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = dict(secrets)

    def get(self, name: str) -> SecretStr:
        value = self.try_get(name)
        if value is None:
            raise MissingSecretError(name)
        return value

    def try_get(self, name: str) -> SecretStr | None:
        raw = self._secrets.get(name)
        return None if raw is None else SecretStr(raw)
