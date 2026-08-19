"""WS-12: §48 is a rule about this repository, so the repository checks it.

An ADR that cannot be traced back to the specification is a decision nobody can
audit later, and a traceability block is exactly the part a hurried author
leaves out. These tests are cheap and they fail in CI rather than in review.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIRECTORY = REPO_ROOT / "doc" / "adr"
SPEC = REPO_ROOT / "doc" / "whatsapp_training_ai_architecture_v1.1.md"

ADRS = sorted(path for path in ADR_DIRECTORY.glob("adr-*.md") if path.name != "template.md")

REQUIRED_SECTIONS = (
    "## Traceability",
    "## Context",
    "## Decision",
    "## Consequences",
    "## Alternatives considered",
    "## How this is enforced",
)

#: The ADRs Sprint 1 commits to, from the sprint plan's WS-12.
EXPECTED_ADRS = {
    "adr-001-modular-monolith.md",
    "adr-003-at-least-once-outbox.md",
    "adr-004-workflow-partitions.md",
    "adr-011-debounce-flush-mechanism.md",
}


def test_the_sprint_shipped_the_records_it_promised() -> None:
    assert {path.name for path in ADRS} == EXPECTED_ADRS


def test_a_template_exists_for_the_next_one() -> None:
    assert (ADR_DIRECTORY / "template.md").is_file()


@pytest.mark.parametrize("adr", ADRS, ids=lambda path: path.name)
def test_every_record_has_the_required_sections(adr: Path) -> None:
    content = adr.read_text(encoding="utf-8")

    for section in REQUIRED_SECTIONS:
        assert section in content, f"{adr.name} is missing {section!r}"


@pytest.mark.parametrize("adr", ADRS, ids=lambda path: path.name)
def test_every_record_traces_back_to_the_specification(adr: Path) -> None:
    """§48: the affected section, the decision record, and the Q numbers."""
    content = adr.read_text(encoding="utf-8")

    assert re.search(r"\*\*Spec sections\*\*\s*\|\s*§\d", content), (
        f"{adr.name} does not name a spec section"
    )
    assert re.search(r"\*\*Decision records\*\*\s*\|\s*DEC-\d{3}", content), (
        f"{adr.name} does not name a DEC"
    )
    assert re.search(r"\*\*Interview questions\*\*\s*\|\s*Q\d", content), (
        f"{adr.name} does not name an interview question"
    )


@pytest.mark.parametrize("adr", ADRS, ids=lambda path: path.name)
def test_every_record_declares_a_status_and_a_date(adr: Path) -> None:
    content = adr.read_text(encoding="utf-8")

    assert re.search(r"\*\*Status:\*\* (Accepted|Proposed|Superseded by ADR-\d{3})", content)
    assert re.search(r"\*\*Date:\*\* \d{4}-\d{2}-\d{2}", content)


@pytest.mark.parametrize("adr", ADRS, ids=lambda path: path.name)
def test_referenced_decision_records_exist_in_the_spec(adr: Path) -> None:
    """A traceability block pointing at a DEC that does not exist is worse than
    none: it looks checked."""
    spec = SPEC.read_text(encoding="utf-8")
    content = adr.read_text(encoding="utf-8")

    for dec in set(re.findall(r"DEC-\d{3}", content)):
        assert f"## {dec} —" in spec, f"{adr.name} references {dec}, which the spec does not define"


@pytest.mark.parametrize("adr", ADRS, ids=lambda path: path.name)
def test_every_record_is_listed_in_the_index(adr: Path) -> None:
    index = (ADR_DIRECTORY / "README.md").read_text(encoding="utf-8")

    assert adr.name in index, f"{adr.name} is not linked from the ADR index"


@pytest.mark.parametrize("adr", ADRS, ids=lambda path: path.name)
def test_consequences_state_a_cost(adr: Path) -> None:
    """An ADR with no costs listed has not been thought through."""
    content = adr.read_text(encoding="utf-8")
    consequences = content.split("## Consequences", 1)[1].split("##", 1)[0]

    assert "**Paid.**" in consequences, f"{adr.name} lists no cost"
