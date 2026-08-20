"""Contract test: the arithmetic behind every stored metric is frozen (Q52).

`derived_metrics.json` holds hand-checked results for each metric version. If a
formula changes, a frozen value stops reproducing and this test fails — and the
only green path is a *new* version key with its own cases, leaving the old ones
intact. That is what makes "changing a formula" a versioning decision rather
than a silent rewrite of every comparison a user has already seen.

Updating a frozen value to match new code defeats the purpose of having it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.domain.training.metrics import (
    METRIC_VERSIONS,
    DerivedMetric,
    compute_by_version,
)

FIXTURE = Path(__file__).parent / "fixtures" / "derived_metrics.json"
GOLDEN: dict[str, list[dict[str, Any]]] = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _compute(version: str, inputs: dict[str, Any]) -> DerivedMetric | None:
    """Dispatch through the same registry production would use.

    Calling the functions directly here would let the fixture keep passing
    after a version was bumped and its dispatch entry forgotten — the fixture
    would be checking arithmetic nothing reaches.
    """
    return compute_by_version(version, inputs)


CASES = [
    pytest.param(version, case, id=f"{version} {index}")
    for version, cases in GOLDEN.items()
    for index, case in enumerate(cases)
]


@pytest.mark.parametrize(("version", "case"), CASES)
def test_every_frozen_case_still_reproduces(version: str, case: dict[str, Any]) -> None:
    metric = _compute(version, case["inputs"])

    assert metric is not None, "a frozen case must produce a metric"
    assert metric.value == Decimal(case["value"]), (
        "the arithmetic changed: bump the version and freeze new cases rather than editing this one"
    )
    assert metric.unit == case["unit"]
    assert metric.version == version


def test_every_shipped_version_is_frozen() -> None:
    """A metric that ships without a frozen case can be changed silently."""
    unfrozen = sorted(set(METRIC_VERSIONS.values()) - set(GOLDEN))

    assert not unfrozen, f"these versions have no frozen cases: {unfrozen}"


def test_every_frozen_version_is_still_shipped() -> None:
    """A version in the fixture that no code produces is an orphan: it looks
    checked and checks nothing."""
    orphaned = sorted(set(GOLDEN) - set(METRIC_VERSIONS.values()))

    assert not orphaned, f"these frozen versions no longer exist in code: {orphaned}"


def test_each_version_freezes_more_than_one_case() -> None:
    """One case can be satisfied by a constant. Several cannot."""
    thin = sorted(version for version, cases in GOLDEN.items() if len(cases) < 2)

    assert not thin, f"these versions are frozen by a single case: {thin}"


def test_version_strings_say_what_the_arithmetic_is() -> None:
    """`1rm.epley.v1` tells a reader which formula produced a stored number;
    `v1` alone would not, and the row outlives the code that wrote it."""
    for name, version in METRIC_VERSIONS.items():
        assert version.endswith(".v1") or ".v" in version
        assert len(version.split(".")) >= 3, f"{name}'s version does not name a method"


def test_every_frozen_version_is_still_computable() -> None:
    """The version-bump path documented at the top of this file only works if
    an old version stays *replayable*. If bumping to v2 made v1's frozen cases
    impossible to compute, the only green path would be deleting them — which
    is exactly the historical freeze this file exists to keep."""
    from app.domain.training.metrics import SUPPORTED_METRIC_VERSIONS

    unreachable = sorted(set(GOLDEN) - set(SUPPORTED_METRIC_VERSIONS))

    assert not unreachable, f"frozen versions with no implementation: {unreachable}"


def test_the_current_version_of_every_metric_is_supported() -> None:
    from app.domain.training.metrics import SUPPORTED_METRIC_VERSIONS

    assert set(METRIC_VERSIONS.values()) <= set(SUPPORTED_METRIC_VERSIONS)


def test_a_stored_row_can_be_recomputed_from_its_version_alone() -> None:
    """This is what a version on a row is *for*: given the inputs and the
    version, the number can be produced again without knowing which formula was
    current at the time."""
    from app.domain.training.metrics import compute_by_version

    metric = compute_by_version(
        "1rm.epley.v1", {"load_kg": "100", "repetitions": 5, "load_mode": "total"}
    )

    assert metric is not None
    assert metric.value == Decimal("116.667")
