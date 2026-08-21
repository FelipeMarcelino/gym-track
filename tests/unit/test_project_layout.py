"""WS-1 smoke tests: the package tree is the one §6 declares, and it is importable.

These are deliberately mechanical. The value is not in what they assert today but
in what they refuse tomorrow: a package quietly renamed away from the spec, a
module that stops importing, or a source directory swallowed by a `.gitignore`
pattern -- which already happened once to `src/app/infrastructure/rabbitmq/`.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "doc" / "whatsapp_training_ai_architecture_v1.1.md"


def _parse_spec_tree() -> tuple[list[str], list[str]]:
    """Extract the §6 directory tree from the spec as (src paths, tests paths).

    The spec is the source of truth for the layout, so the test reads it rather
    than restating it -- a restated list drifts silently, a parsed one cannot.
    """
    lines = SPEC.read_text(encoding="utf-8").splitlines()
    start = lines.index("# 6. Project Structure")
    block_start = lines.index("```text", start) + 1
    block_end = lines.index("```", block_start)

    src_paths: list[str] = []
    test_paths: list[str] = []
    stack: dict[int, str] = {}
    root = ""

    for raw in lines[block_start:block_end]:
        line = raw.rstrip()
        if not line:
            continue
        if not line.startswith((" ", "│", "├", "└")):
            root = line.rstrip("/")
            stack = {}
            continue

        name = line.split("── ", 1)[1].rstrip("/")
        # Each nesting level of the drawing is exactly four columns wide.
        depth = line.index("── ") // 4
        parent = stack.get(depth - 1, "")
        path = f"{parent}/{name}" if parent else name
        stack[depth] = path
        stack.pop(depth + 1, None)

        if root.startswith("src/"):
            src_paths.append(f"{root}/{path}")
        else:
            test_paths.append(f"{root}/{path}")

    return src_paths, test_paths


SRC_PACKAGES, TEST_PACKAGES = _parse_spec_tree()


def test_spec_tree_was_parsed() -> None:
    """Guard the parser itself: an empty parse would make every test below vacuous."""
    assert len(SRC_PACKAGES) >= 40
    assert len(TEST_PACKAGES) >= 10
    assert "src/app/infrastructure/rabbitmq" in SRC_PACKAGES
    assert "tests/e2e" in TEST_PACKAGES


@pytest.mark.parametrize("package_path", SRC_PACKAGES + TEST_PACKAGES)
def test_declared_package_exists(package_path: str) -> None:
    directory = REPO_ROOT / package_path
    assert directory.is_dir(), f"§6 declares {package_path}, which does not exist"
    assert (directory / "__init__.py").is_file(), f"{package_path} is not a package"


@pytest.mark.parametrize("package_path", SRC_PACKAGES)
def test_declared_package_imports(package_path: str) -> None:
    module_name = package_path.removeprefix("src/").replace("/", ".")
    assert importlib.import_module(module_name).__name__ == module_name


def test_no_source_path_is_gitignored() -> None:
    """A `.gitignore` pattern must never swallow a source or test directory.

    `rabbitmq/`, `lib/`, `var/` and `target/` all ship unanchored in the GitHub
    Python template, and `rabbitmq/` really did hide an infrastructure package
    from every `git add` until it was anchored.
    """
    candidates = "\n".join(SRC_PACKAGES + TEST_PACKAGES)
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        input=candidates,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    # check-ignore exits 1 when nothing matched, which is the outcome we want.
    assert result.returncode == 1, f"gitignored source paths: {result.stdout.split()}"


async def test_async_test_runner_is_wired() -> None:
    """pytest-asyncio in auto mode: an `async def` test must actually run."""
    await asyncio.sleep(0)
    assert asyncio.get_running_loop().is_running()


ORCHESTRATION_PACKAGES = ("langgraph", "langchain", "langchain_core")

#: Where an orchestration import is allowed to appear. LangGraph is an adapter:
#: the domain must stay pure (it already may not import SQLAlchemy) and the
#: application layer must keep talking through its own ports, or Sprint 4 swaps
#: a node and finds the coupling everywhere.
ORCHESTRATION_ALLOWED = (
    "src/app/graphs/",
    "src/app/infrastructure/langgraph/",
)


def _python_sources() -> list[Path]:
    return sorted((REPO_ROOT / "src" / "app").rglob("*.py"))


def _imported_roots(source: Path) -> set[str]:
    import ast

    tree = ast.parse(source.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("source", _python_sources(), ids=lambda path: path.name)
def test_only_the_graph_package_imports_the_orchestration_library(source: Path) -> None:
    relative = source.relative_to(REPO_ROOT).as_posix()
    if relative.startswith(ORCHESTRATION_ALLOWED):
        return

    offending = _imported_roots(source) & set(ORCHESTRATION_PACKAGES)
    assert not offending, (
        f"{relative} imports {sorted(offending)}; LangGraph is an adapter and "
        "belongs behind app/graphs/ or app/infrastructure/langgraph/"
    )
