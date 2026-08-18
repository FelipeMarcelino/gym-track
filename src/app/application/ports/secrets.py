"""Port through which every secret value enters the process (Q144).

Nothing in the codebase reads a secret from the environment directly. The
indirection exists so that swapping the local environment for a cloud secrets
manager is a change of one binding at the composition root, not a change spread
across every module that happens to need a password.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import SecretStr


class MissingSecretError(LookupError):
    """A secret the process needs is not available from the provider."""

    def __init__(self, name: str) -> None:
        super().__init__(f"secret {name!r} is not available from the secrets provider")
        self.name = name


@runtime_checkable
class SecretsProvider(Protocol):
    """Resolves a secret by logical name, e.g. ``postgres/api/password``.

    Names are provider-agnostic paths. Each implementation maps them onto its
    own backend naming, so the same name works against the environment locally
    and against a managed secrets store in production.
    """

    def get(self, name: str) -> SecretStr:
        """Return the secret, raising :class:`MissingSecretError` if absent."""
        ...

    def try_get(self, name: str) -> SecretStr | None:
        """Return the secret, or ``None`` when the provider does not have it."""
        ...
