"""Carrying a stated value forward to the sets that follow it (§14.4, Q54).

"80kg: 10, 9, 8" is one stated load and three stated rep counts. Filling in the
later loads is what makes the sentence mean what the user meant. Filling in the
later reps would invent training they never described — and a missing rep count
is Q46's clarification case, not a gap.

Every carried value is marked INHERITED. §14.4 needs the distinction because a
correction later has to know whose number it is fixing: the user's typo, or our
inference.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.training.activities import LoadMode, SetType
from app.domain.training.input_contract import StructuredSetInput
from app.domain.training.provenance import Provenance


@dataclass(frozen=True, slots=True)
class InheritedValue[T]:
    value: T | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class InheritedSet:
    repetitions: InheritedValue[int]
    load: InheritedValue[str]
    load_mode: InheritedValue[LoadMode]
    distance: InheritedValue[str]
    duration: InheritedValue[str]
    set_type: SetType
    #: Never inherited (§14.3): "a última foi difícil" is a claim about the
    #: last set, and carrying it forward records an effort nobody reported.
    effort: str | None
    notes: str | None


def _carried[T](stated: T | None, previous: T | None) -> InheritedValue[T]:
    if stated is not None:
        return InheritedValue(value=stated, provenance=Provenance.EXPLICIT)
    if previous is not None:
        return InheritedValue(value=previous, provenance=Provenance.INHERITED)
    # Nothing to carry. EXPLICIT rather than INHERITED because nothing was
    # inferred here -- the value is simply absent, and the validator decides
    # whether that matters.
    return InheritedValue(value=None, provenance=Provenance.EXPLICIT)


def inherit_within_block(sets: Sequence[StructuredSetInput]) -> tuple[InheritedSet, ...]:
    """Resolve each set against the ones before it, within one block."""
    resolved: list[InheritedSet] = []
    load: str | None = None
    load_mode: LoadMode | None = None
    distance: str | None = None
    duration: str | None = None

    for stated in sets:
        current = InheritedSet(
            # Stated or absent, never carried.
            repetitions=InheritedValue(value=stated.repetitions, provenance=Provenance.EXPLICIT),
            load=_carried(stated.load, load),
            load_mode=_carried(stated.load_mode, load_mode),
            distance=_carried(stated.distance, distance),
            duration=_carried(stated.duration, duration),
            set_type=stated.set_type,
            effort=stated.effort,
            notes=stated.notes,
        )
        resolved.append(current)
        load = current.load.value
        load_mode = current.load_mode.value
        distance = current.distance.value
        duration = current.duration.value

    return tuple(resolved)
