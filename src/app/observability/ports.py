"""Metrics and AI-tracing ports, with no-op implementations (DEC-013).

Application and AI observability stay separable from day one: Datadog answers
"is the system healthy", Langfuse answers "is the model behaving". Collapsing
them into one interface now would make splitting them later a refactor of every
call site.

Both no-ops exist so that code written this sprint can already call them, and
so that a test can assert *what* would have been emitted without a backend.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

Tags = Mapping[str, str]


class MetricsPort(Protocol):
    def increment(self, name: str, *, value: int = 1, tags: Tags | None = None) -> None: ...

    def gauge(self, name: str, value: float, *, tags: Tags | None = None) -> None: ...

    def timing(self, name: str, milliseconds: float, *, tags: Tags | None = None) -> None: ...


class AITracingPort(Protocol):
    """Model-facing observability (§30.2). No consumer until Sprint 3."""

    def record_generation(
        self,
        name: str,
        *,
        model: str,
        prompt_version: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None: ...


class NoOpMetrics:
    def increment(self, name: str, *, value: int = 1, tags: Tags | None = None) -> None:
        return None

    def gauge(self, name: str, value: float, *, tags: Tags | None = None) -> None:
        return None

    def timing(self, name: str, milliseconds: float, *, tags: Tags | None = None) -> None:
        return None


class NoOpAITracing:
    def record_generation(
        self,
        name: str,
        *,
        model: str,
        prompt_version: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        return None
