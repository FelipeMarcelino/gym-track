"""WS-10: the strict-syntax adapter (D7, ADR-013).

A rigid parser that exists so Sprint 2 can be exercised end to end without a
language model. Its whole discipline is literalness: it does not know
synonyms, does not infer a missing rep count, and does not accept "3x10",
because that is natural language and the extractor's job in Sprint 3.

The no-invention test at the bottom is the one that matters. Every value the
parser produces has to appear in the line the user typed — that is the
mechanical form of the promise the whole sprint makes.
"""

from __future__ import annotations

import pytest

from app.application.services.strict_syntax import (
    LOG_PREFIX,
    StrictSyntaxError,
    matches,
    parse,
)
from app.domain.training.input_contract import StructuredWorkoutInput

GOOD_LINES = [
    "#log supino 80kg 10 9 8",
    "#log supino 80kg",
    "#log flexao 10",
    "#log supino 80kg 10 @RPE8",
    "#log corrida",
    "#log rosca direta 20kg 12 12",
]


def test_the_prefix_is_what_selects_this_parser() -> None:
    assert matches("#log supino 80kg 10")
    assert matches("#LOG supino")
    assert matches("  #log supino")
    assert not matches("bom dia")
    assert not matches("log supino")


def test_text_without_the_prefix_is_not_this_parsers_business() -> None:
    """Returning None rather than raising: the caller falls through to the
    acknowledgement path, and a greeting is not a syntax error."""
    assert parse(["bom dia", "tudo bem?"]) is None


def test_a_load_and_three_rep_counts_become_three_sets() -> None:
    """The load is stated once. Carrying it to the later sets is WS-8's job —
    a parser that did it here would be inventing on line one."""
    parsed = parse(["#log supino 80kg 10 9 8"])

    assert parsed is not None
    activity = parsed.activities[0]
    assert activity.raw_name == "supino"
    assert [item.repetitions for item in activity.sets] == [10, 9, 8]
    assert [item.load for item in activity.sets] == ["80kg", None, None]


def test_a_load_with_no_reps_is_a_set_that_is_missing_something() -> None:
    """`repetitions is None` is the input WS-2 turns into a clarification. The
    adapter's job is to be literal, not helpful."""
    parsed = parse(["#log supino 80kg"])

    assert parsed is not None
    a_set = parsed.activities[0].sets[0]
    assert a_set.load == "80kg"
    assert a_set.repetitions is None


def test_reps_with_no_load_is_a_bodyweight_shaped_set() -> None:
    parsed = parse(["#log flexao 10"])

    assert parsed is not None
    a_set = parsed.activities[0].sets[0]
    assert a_set.repetitions == 10
    assert a_set.load is None


def test_a_trailing_effort_lands_on_the_set_that_carries_it() -> None:
    parsed = parse(["#log supino 80kg 10 @RPE8"])

    assert parsed is not None
    assert parsed.activities[0].sets[0].effort == "RPE8"


def test_an_exercise_with_nothing_after_it_is_still_an_activity() -> None:
    """ "#log corrida" says the user ran. What is missing becomes a question,
    not a parse failure — they typed the marker and meant something by it."""
    parsed = parse(["#log corrida"])

    assert parsed is not None
    assert parsed.activities[0].raw_name == "corrida"
    assert parsed.activities[0].sets == ()


def test_a_multi_word_exercise_keeps_its_words() -> None:
    parsed = parse(["#log rosca direta 20kg 12 12"])

    assert parsed is not None
    assert parsed.activities[0].raw_name == "rosca direta"


def test_two_logged_lines_are_two_activities_in_order() -> None:
    parsed = parse(["#log supino 80kg 10", "#log agachamento 100kg 5"])

    assert parsed is not None
    assert [activity.raw_name for activity in parsed.activities] == ["supino", "agachamento"]


def test_unmarked_lines_in_the_batch_are_kept_rather_than_dropped() -> None:
    """Nothing the user said disappears silently. The domain never reads it;
    it is there so a later sprint can ask instead of finding it lost."""
    parsed = parse(["bom dia", "#log supino 80kg 10", "acho que foi isso"])

    assert parsed is not None
    assert len(parsed.activities) == 1
    assert parsed.unparsed == ("bom dia", "acho que foi isso")


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("#log", "exercise"),
        ("#log 80kg 10", "exercise"),
        ("#log supino 3x10", "3x10"),
        ("#log supino 80kg 10 @", "effort"),
    ],
)
def test_a_marked_line_that_does_not_parse_says_so(line: str, problem: str) -> None:
    """The user typed the marker, so they get told what was wrong rather than
    silently receiving an acknowledgement for a workout nobody recorded."""
    with pytest.raises(StrictSyntaxError, match=problem):
        parse([line])


def test_the_x_notation_is_refused_on_purpose() -> None:
    """ "3x10" is natural language. ADR-013 draws the line here: this adapter
    stays rigid and dies in Sprint 3 rather than growing toward the extractor
    it is standing in for."""
    with pytest.raises(StrictSyntaxError):
        parse(["#log supino 3x10 80kg"])


# --------------------------------------------------------------------------
# The promise
# --------------------------------------------------------------------------


@pytest.mark.parametrize("line", GOOD_LINES)
def test_no_value_is_invented(line: str) -> None:
    """Every value the parser produces appears in the line the user typed.

    The mechanical form of the sprint's promise: this adapter reports what was
    written and nothing else. A default that leaked in here would be a number
    in the user's history that they never said and cannot recognize.
    """
    parsed = parse([line])
    assert parsed is not None

    haystack = line.casefold()
    for activity in parsed.activities:
        assert activity.raw_name.casefold() in haystack
        for a_set in activity.sets:
            for value in (a_set.load, a_set.effort, a_set.distance, a_set.duration):
                if value is not None:
                    assert value.casefold() in haystack
            if a_set.repetitions is not None:
                assert str(a_set.repetitions) in haystack


@pytest.mark.parametrize("line", GOOD_LINES)
def test_everything_it_produces_satisfies_the_contract(line: str) -> None:
    """The adapter is the first producer written against WS-8's contract, so
    it is also the first proof that the contract can be produced."""
    parsed = parse([line])

    assert isinstance(parsed, StructuredWorkoutInput)
    assert parsed.schema_version == "workout-input.v1"


def test_the_prefix_is_the_one_the_readme_documents() -> None:
    assert LOG_PREFIX == "#log"
