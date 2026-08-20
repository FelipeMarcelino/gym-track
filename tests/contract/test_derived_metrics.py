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

from app.domain.training.activities import LoadMode
from app.domain.training.metrics import (
    METRIC_VERSIONS,
    ONE_RM_VERSION,
    PACE_VERSION,
    SPEED_VERSION,
    VOLUME_VERSION,
    DerivedMetric,
    estimated_one_rm,
    pace,
    speed,
    volume,
)

FIXTURE = Path(__file__).parent / "fixtures" / "derived_metrics.json"
GOLDEN: dict[str, list[dict[str, Any]]] = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _compute(version: str, inputs: dict[str, Any]) -> DerivedMetric | None:
    """Call the function that owns a version, with the fixture's inputs."""
    match version:
        case _ if version == VOLUME_VERSION:
            return volume(
                load_kg=Decimal(inputs["load_kg"]),
                repetitions=int(inputs["repetitions"]),
                load_mode=LoadMode(inputs["load_mode"]),
            )
        case _ if version == ONE_RM_VERSION:
            return estimated_one_rm(
                load_kg=Decimal(inputs["load_kg"]),
                repetitions=int(inputs["repetitions"]),
            )
        case _ if version == PACE_VERSION:
            return pace(
                distance_m=Decimal(inputs["distance_m"]),
                duration_s=Decimal(inputs["duration_s"]),
            )
        case _ if version == SPEED_VERSION:
            return speed(
                distance_m=Decimal(inputs["distance_m"]),
                duration_s=Decimal(inputs["duration_s"]),
            )
        case _:
            raise AssertionError(f"the fixture has no way to compute {version!r}")


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
