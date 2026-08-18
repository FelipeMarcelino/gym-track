"""Smoke tests for the project skeleton (WS-1).

These guard the two things that silently break a fresh checkout: the package
being unimportable, and a directory being added under ``src/app`` without an
``__init__.py`` so it never ships in the wheel.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import app

SRC_ROOT = Path(app.__file__).resolve().parent


def _package_dirs() -> list[Path]:
    return sorted(
        path for path in SRC_ROOT.rglob("*") if path.is_dir() and path.name != "__pycache__"
    )


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC_ROOT)
    return ".".join(("app", *relative.parts))


def test_app_package_is_importable() -> None:
    assert app.__file__ is not None
    assert SRC_ROOT.name == "app"


@pytest.mark.parametrize("package_dir", _package_dirs(), ids=_module_name)
def test_every_directory_is_an_importable_package(package_dir: Path) -> None:
    """A directory without ``__init__.py`` is excluded from the built wheel."""
    assert (package_dir / "__init__.py").is_file(), (
        f"{package_dir} has no __init__.py and will not ship in the wheel"
    )
    importlib.import_module(_module_name(package_dir))
