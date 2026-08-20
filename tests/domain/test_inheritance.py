"""WS-8: carrying a stated value forward to the sets that follow (§14.4, Q54).

"80kg: 10, 9, 8" is one stated load and three stated rep counts. Filling the
later loads in is what makes the sentence mean what the user meant; filling the
later *reps* in would invent training they never described. The asymmetry is
the whole module.
"""

from __future__ import annotations

import pytest

from app.domain.training.activities import SetType
from app.domain.training.inheritance import inherit_within_block
from app.domain.training.input_contract import StructuredSetInput
from app.domain.training.provenance import Provenance


def test_a_stated_load_carries_forward_and_says_that_it_did() -> None:
    """Q54: the load applies to the sets that follow it, and each carried value
    is marked INHERITED so a later correction knows whose number it is fixing."""
    sets = inherit_within_block(
        (
            StructuredSetInput(load="80kg", repetitions=10),
            StructuredSetInput(repetitions=9),
            StructuredSetInput(repetitions=8),
        )
    )

    assert [item.load.value for item in sets] == ["80kg", "80kg", "80kg"]
    assert [item.load.provenance for item in sets] == [
        Provenance.EXPLICIT,
        Provenance.INHERITED,
        Provenance.INHERITED,
    ]


def test_repetitions_are_never_inherited() -> None:
    """A missing rep count is the clarification case (Q46), not a gap to fill:
    inventing one writes training the user never did."""
    sets = inherit_within_block(
        (StructuredSetInput(load="80kg", repetitions=10), StructuredSetInput())
    )

    assert sets[1].repetitions.value is None
    assert sets[1].repetitions.provenance is Provenance.EXPLICIT


def test_a_restated_value_starts_the_chain_again() -> None:
    """Dropping the weight mid-exercise is normal, and the sets after it must
    not keep reporting the old one."""
    sets = inherit_within_block(
        (
            StructuredSetInput(load="80kg", repetitions=10),
            StructuredSetInput(repetitions=9),
            StructuredSetInput(load="70kg", repetitions=8),
            StructuredSetInput(repetitions=8),
        )
    )

    assert [item.load.value for item in sets] == ["80kg", "80kg", "70kg", "70kg"]
    assert [item.load.provenance for item in sets] == [
        Provenance.EXPLICIT,
        Provenance.INHERITED,
        Provenance.EXPLICIT,
        Provenance.INHERITED,
    ]


def test_effort_belongs_to_the_set_that_reported_it() -> None:
    """§14.3: "a última foi difícil" is a claim about the last set. Carrying it
    forward would record an effort the user never reported for the others."""
    sets = inherit_within_block(
        (
            StructuredSetInput(repetitions=10, effort="RPE 8"),
            StructuredSetInput(repetitions=9),
        )
    )

    assert sets[0].effort == "RPE 8"
    assert sets[1].effort is None


def test_distance_and_duration_carry_forward_like_load() -> None:
    """ "3 séries de 400m" is one stated distance and three intervals."""
    sets = inherit_within_block(
        (
            StructuredSetInput(distance="400m", duration="90s"),
            StructuredSetInput(),
            StructuredSetInput(),
        )
    )

    assert [item.distance.value for item in sets] == ["400m", "400m", "400m"]
    assert [item.duration.value for item in sets] == ["90s", "90s", "90s"]
    assert sets[2].distance.provenance is Provenance.INHERITED


def test_the_set_type_is_kept_per_set() -> None:
    """A warmup followed by working sets is the normal shape of an exercise,
    and inheriting the type would relabel real work as a warmup."""
    sets = inherit_within_block(
        (
            StructuredSetInput(load="40kg", repetitions=12, set_type=SetType.WARMUP),
            StructuredSetInput(repetitions=10),
        )
    )

    assert sets[0].set_type is SetType.WARMUP
    assert sets[1].set_type is SetType.WORKING


def test_nothing_stated_inherits_nothing() -> None:
    sets = inherit_within_block((StructuredSetInput(), StructuredSetInput()))

    assert all(item.load.value is None for item in sets)
    assert all(item.load.provenance is Provenance.EXPLICIT for item in sets)


@pytest.mark.parametrize("empty", [(), []])
def test_an_empty_block_inherits_nothing_and_raises_nothing(empty: object) -> None:
    assert inherit_within_block(empty) == ()  # type: ignore[arg-type]
