"""WS-7: turning what somebody typed into something matchable (§16).

Two layers depend on this agreeing with itself: the catalog stores this form so
a lookup is an index seek, and the resolver computes it on the way in. If they
drifted, an alias would be stored in one shape and searched for in another, and
resolution would fail for a reason invisible in either file.
"""

from __future__ import annotations

import pytest

from app.domain.exercises.normalization import normalize_for_match


@pytest.mark.parametrize(
    "raw",
    ["Supino Reto", "supino  reto", "SUPINO-RETO", "supíno reto", "  supino reto  ", "supino/reto"],
)
def test_spellings_a_human_reads_the_same_normalize_the_same(raw: str) -> None:
    """Half of Brazilian typing omits the accents and the other half does not,
    and both halves mean the same exercise."""
    assert normalize_for_match(raw) == "supino reto"


@pytest.mark.parametrize("raw", ["", "   ", "---", "!!!", "  ...  "])
def test_input_with_nothing_to_match_normalizes_to_nothing(raw: str) -> None:
    """An empty result is the honest answer, and the caller must not be able to
    mistake it for a term that happens to match everything."""
    assert normalize_for_match(raw) == ""


def test_normalization_is_idempotent() -> None:
    """Applied twice, because the catalog applies it at write time and the
    resolver applies it again at read time."""
    once = normalize_for_match("Levantamento Terra Romeno")
    assert normalize_for_match(once) == once
