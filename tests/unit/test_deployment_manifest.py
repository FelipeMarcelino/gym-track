"""WS-1: what `docker compose up` actually runs, asserted cheaply.

`make migrate` and the compose `migrate` service are two different paths to the
same job, and only one of them goes through the Makefile. The checkpoint tables
are created by a script rather than by a migration, so a compose service that
runs `alembic upgrade head` alone leaves a fresh stack with the schema and none
of its tables -- and the worker role cannot create them.

That failure only shows up on an empty volume, which is the worst place to
discover it. This test costs nothing and finds it in CI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker" / "compose.yaml"
MAKEFILE = REPO_ROOT / "Makefile"

SETUP_MODULE = "app.infrastructure.langgraph.checkpointer"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    loaded = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_compose_migration_creates_the_checkpoint_tables(compose: dict[str, Any]) -> None:
    command = compose["services"]["migrate"]["command"]
    rendered = " ".join(command) if isinstance(command, list) else str(command)

    assert "alembic upgrade head" in rendered
    assert SETUP_MODULE in rendered, (
        "the compose migrate service must create the checkpoint tables too; "
        "a fresh volume otherwise starts the worker against an empty schema"
    )


def test_make_migrate_creates_them_as_well() -> None:
    body = MAKEFILE.read_text(encoding="utf-8")
    target = body.split("\nmigrate:", 1)[1].split("\n\n", 1)[0]

    assert "alembic upgrade head" in target
    assert SETUP_MODULE in target


def test_every_application_service_waits_for_the_migration(compose: dict[str, Any]) -> None:
    """Otherwise the ordering the previous tests establish is decorative."""
    services = compose["services"]
    application = {
        name
        for name, spec in services.items()
        if name not in {"postgres", "rabbitmq", "redis", "migrate"} and "command" in spec
    }
    assert application, "expected the compose file to define application services"

    for name in sorted(application):
        depends = services[name].get("depends_on", {})
        assert "migrate" in depends, f"{name} does not wait for migrate"
