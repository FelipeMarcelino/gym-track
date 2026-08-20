"""A rigid logging syntax, so Sprint 2 can be exercised without a model (D7).

This file is temporary. Sprint 3 replaces it with the WorkoutExtractor, and its
contract test becomes that extractor's first eval case. Until then it is the
only producer of `StructuredWorkoutInput`, which makes it the proof that the
contract can actually be produced.

Its discipline is literalness. It knows no synonyms, infers no missing rep
count, and refuses "3x10" — that is natural language, and ADR-013 draws the
line here deliberately: an adapter that grows toward the extractor it stands in
for is an adapter nobody ever deletes. `#log supino 80kg` produces a set with
no repetitions, which is exactly the input WS-2 turns into a question. Being
helpful here would mean inventing a number the user never said.

    #log <exercise words> [<load>] [<reps> ...] [@<effort>]

Every token type is distinguishable by shape: a load carries a unit suffix,
reps are bare integers, effort is prefixed with `@`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from app.domain.training.input_contract import (
    StructuredActivityInput,
    StructuredSetInput,
    StructuredWorkoutInput,
)

LOG_PREFIX: Final = "#log"

#: A number glued to letters: "80kg", "176lb", "5km", "1500m".
_LOAD_LIKE = re.compile(r"^\d+(?:[.,]\d+)?[a-z]+$", re.IGNORECASE)
#: A bare integer: one set's repetitions.
_REPS = re.compile(r"^\d+$")
#: "3x10" and friends. Recognized only so it can be refused by name.
# The multiplication sign is deliberate: phone keyboards produce it, and a
# line this parser cannot read is worth refusing by name either way.
_SETS_BY_REPS = re.compile(r"^\d+\s*[x×]\s*\d+$", re.IGNORECASE)  # noqa: RUF001


class StrictSyntaxError(ValueError):
    """A line began with the prefix and did not parse.

    Never a silent skip: the user typed the marker, so they are told what was
    wrong instead of receiving an acknowledgement for a workout nobody wrote.
    """

    def __init__(self, raw: str, problem: str) -> None:
        super().__init__(f"could not parse {raw!r}: {problem}")
        self.raw = raw
        self.problem = problem


def matches(text: str) -> bool:
    """Whether this line is addressed to this parser.

    The prefix has to be the whole first token. A bare `startswith` also
    claims `#logger` and `#logbook`, which are ordinary hashtags -- and this
    parser would strip four characters off one and read the rest as a workout.
    """
    stripped = text.strip().casefold()
    if not stripped.startswith(LOG_PREFIX):
        return False
    remainder = stripped[len(LOG_PREFIX) :]
    return not remainder or remainder[0].isspace()


def parse(texts: Sequence[str]) -> StructuredWorkoutInput | None:
    """Every `#log` line in a batch, as one workout.

    Returns None when no line carries the prefix, so the caller falls through
    to the acknowledgement path — a greeting is not a syntax error.
    """
    activities: list[StructuredActivityInput] = []
    unparsed: list[str] = []

    for text in texts:
        if matches(text):
            activities.append(_activity(text))
        else:
            # Kept rather than dropped: nothing the user said disappears
            # silently, even when the domain will never read it.
            unparsed.append(text)

    if not activities:
        return None

    return StructuredWorkoutInput(activities=tuple(activities), unparsed=tuple(unparsed))


def _activity(line: str) -> StructuredActivityInput:
    stripped = line.strip()
    tokens = stripped[len(LOG_PREFIX) :].split()

    name_words: list[str] = []
    load: str | None = None
    repetitions: list[int] = []
    effort: str | None = None

    # Counted up front so a line with two of them is told it has two, rather
    # than being told the first one is in the wrong place.
    marked = [token for token in tokens if token.startswith("@")]
    if len(marked) > 1:
        raise StrictSyntaxError(
            stripped, f"two efforts in one line: {marked[0][1:]!r} and {marked[1][1:]!r}"
        )

    for position, token in enumerate(tokens):
        if _SETS_BY_REPS.match(token):
            raise StrictSyntaxError(
                stripped,
                f"{token!r} is natural language, and this syntax is positional; "
                "write the repetitions one per set",
            )
        if token.startswith("@"):
            if position != len(tokens) - 1:
                # The grammar puts the effort last. Accepting it anywhere would
                # make the syntax positional in the README and not in the
                # parser, which is the drift ADR-013 exists to prevent.
                raise StrictSyntaxError(
                    stripped, f"the effort {token!r} must be the last token on the line"
                )
            effort = token[1:]
            if not effort:
                raise StrictSyntaxError(stripped, "an effort marker with no effort after it")
            continue
        if _REPS.match(token):
            repetitions.append(int(token))
            continue
        if _LOAD_LIKE.match(token):
            if load is not None:
                raise StrictSyntaxError(stripped, f"two loads in one line: {load!r} and {token!r}")
            load = token
            continue
        if repetitions or load:
            # Words after the numbers started have no position in this grammar,
            # and guessing at one is how a positional parser starts becoming a
            # language.
            raise StrictSyntaxError(stripped, f"unexpected word {token!r} after the numbers")
        name_words.append(token)

    if not name_words:
        raise StrictSyntaxError(stripped, "no exercise name")

    return StructuredActivityInput(
        raw_name=" ".join(name_words),
        sets=_sets(load, repetitions, effort),
    )


def _sets(
    load: str | None, repetitions: Sequence[int], effort: str | None
) -> tuple[StructuredSetInput, ...]:
    """One set per rep count, or a single set when only a load was given.

    The load is attached to the first set only. Carrying it forward is WS-8's
    inheritance, which records that it was carried; doing it here would lose
    the distinction §14.4 exists to keep.
    """
    if not repetitions:
        if load is None and effort is None:
            # "#log corrida" — the user said they did something. What is
            # missing becomes a question, not a parse failure.
            return ()
        return (StructuredSetInput(load=load, effort=effort),)

    return tuple(
        StructuredSetInput(
            repetitions=count,
            load=load if index == 0 else None,
            effort=effort if index == 0 else None,
        )
        for index, count in enumerate(repetitions)
    )
