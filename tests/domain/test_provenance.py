"""WS-6: the three vocabularies the workout schema stores (§14.4, §26.2, Q51).

Small on purpose. They are pinned here because the values are written into
columns: renaming one is a migration, and a test that fails is a cheaper way to
learn that than a production row nobody can deserialize.
"""

from __future__ import annotations

from app.domain.training.provenance import ExerciseGroupType, Provenance, SourceRole


def test_a_value_is_either_stated_or_carried_forward() -> None:
    """§14.4: "3x10 60kg, depois mais 2 séries" states the reps of the last two
    sets and inherits their load. Two members, because a third would mean the
    service is guessing at a distinction the user never made."""
    assert {member.value for member in Provenance} == {"explicit", "inherited"}


def test_a_source_says_how_the_message_relates_to_the_row() -> None:
    """§26.2: created, updated, or answered a question — the three ways a
    message can be the reason a row looks the way it does."""
    assert {member.value for member in SourceRole} == {
        "created_from",
        "updated_from",
        "clarified_by",
    }


def test_the_group_types_are_the_ones_q51_names() -> None:
    assert {member.value for member in ExerciseGroupType} == {
        "superset",
        "triset",
        "circuit",
        "complex",
    }
