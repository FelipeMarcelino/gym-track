"""Effort normalization, deterministic half (§14.3).

RPE is the user-facing scale, and input arrives as an explicit RPE, as reps in
reserve, or as a phrase. This module reads the first two exactly and the third
from a small curated table.

**A phrase the table does not contain is stored raw with no RPE.** §14.3
describes a small LLM classifier as the fallback for those, and it arrives in
Sprint 3 — the sprint plan records this as a deliberate deviation. Until then,
guessing that "foi osso" means 8.5 would put a number into the history that the
user never gave, and nothing downstream could tell it apart from a reported one.

The raw text, the normalized value, the method and the version are all kept,
because §14.3 requires a stored RPE to be re-readable knowing how it was made.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from app.domain.exercises.normalization import normalize_for_match

EFFORT_VERSION: Final = "effort.deterministic.v1"

#: RPE is a 1-10 scale. Zero is not "no effort", it is a value nobody reports.
MINIMUM_RPE: Final = Decimal(1)
MAXIMUM_RPE: Final = Decimal(10)


class EffortMethod(StrEnum):
    EXPLICIT_RPE = "explicit_rpe"
    RIR_MAPPED = "rir_mapped"
    PHRASE_TABLE = "phrase_table"
    #: Stored raw, RPE left empty. The honest outcome for anything this
    #: deterministic half cannot read.
    UNNORMALIZED = "unnormalized"


#: Reps in reserve to RPE. Frozen and enumerated by its test: the table is a
#: claim about six distinct inputs, not a formula.
#:
#: It stops at five because past that the mapping stops meaning anything --
#: "six or more left in the tank" is not a distinct level of effort, and
#: extrapolating would invent the part nobody agreed on.
RIR_TO_RPE: Final[Mapping[int, Decimal]] = {
    0: Decimal("10"),
    1: Decimal("9"),
    2: Decimal("8"),
    3: Decimal("7"),
    4: Decimal("6"),
    5: Decimal("5"),
}

#: Curated pt-BR phrases, deliberately small: every entry is a claim about what
#: a user meant, and a large table is a large number of unverified claims.
#: Keys are already in normalized form, so lookup is a single comparison.
PT_BR_EFFORT_PHRASES: Final[Mapping[str, Decimal]] = {
    "leve": Decimal("5"),
    "tranquilo": Decimal("6"),
    "moderado": Decimal("7"),
    "puxado": Decimal("8"),
    "pesado": Decimal("8.5"),
    "quase falhei": Decimal("9.5"),
    "falhei": Decimal("10"),
    "ate a falha": Decimal("10"),
}

_RPE_PATTERN = re.compile(r"^(?:rpe\s*(?P<a>-?\d+(?:[.,]\d+)?)|(?P<b>-?\d+(?:[.,]\d+)?)\s*rpe)$")
#: No sign: reps in reserve are never negative, and `int("-0")` is zero -- a
#: signed zero would otherwise map to RPE 10, promoting a typo to a maximal
#: effort measurement.
_RIR_PATTERN = re.compile(r"^(?:rir\s*(?P<a>\d+)|(?P<b>\d+)\s*rir)$")


@dataclass(frozen=True, slots=True)
class NormalizedEffort:
    raw: str
    rpe: Decimal | None
    method: EffortMethod
    version: str = EFFORT_VERSION

    @property
    def is_normalized(self) -> bool:
        return self.rpe is not None


class EffortNormalizer:
    def __init__(self, phrases: Mapping[str, Decimal] | None = None) -> None:
        # Keys are normalized on the way in, so an injected table can be
        # written the way a person writes -- "Até a Falha" -- and still match.
        # Requiring callers to pre-normalize would make the extension point
        # work only for whoever knew the rule, and fail silently for everyone
        # else.
        source = PT_BR_EFFORT_PHRASES if phrases is None else phrases
        self._phrases = {normalize_for_match(phrase): rpe for phrase, rpe in source.items()}

    def normalize(self, raw: str | None) -> NormalizedEffort | None:
        """Read an effort, or record that it could not be read.

        Returns `None` only when nothing was stated. "No effort was reported"
        and "an effort was reported that we could not read" are different facts
        about a set, and collapsing them would lose the second one entirely.
        """
        if raw is None or not raw.strip():
            return None

        # Numbers are read from the text as typed, only lowercased. The phrase
        # normalizer strips punctuation, which would turn "8,5" into "8 5" and
        # "-3" into "3" -- the second one silently converting a nonsense value
        # into a plausible one.
        numeric = raw.strip().casefold()

        explicit = self._explicit_rpe(numeric)
        if explicit is not None:
            return NormalizedEffort(raw=raw, rpe=explicit, method=EffortMethod.EXPLICIT_RPE)

        mapped = self._from_rir(numeric)
        if mapped is not None:
            return NormalizedEffort(raw=raw, rpe=mapped, method=EffortMethod.RIR_MAPPED)

        from_table = self._phrases.get(normalize_for_match(raw))
        if from_table is not None:
            return NormalizedEffort(raw=raw, rpe=from_table, method=EffortMethod.PHRASE_TABLE)

        return NormalizedEffort(raw=raw, rpe=None, method=EffortMethod.UNNORMALIZED)

    def _explicit_rpe(self, text: str) -> Decimal | None:
        match = _RPE_PATTERN.match(text)
        if match is None:
            return None

        try:
            value = Decimal((match.group("a") or match.group("b")).replace(",", "."))
        except InvalidOperation:  # pragma: no cover - the pattern already filtered
            return None

        # Outside the scale is a typo or a joke, not a reading: an RPE of 42
        # stored in an RPE column is a value no analysis can interpret.
        if not MINIMUM_RPE <= value <= MAXIMUM_RPE:
            return None
        return value

    def _from_rir(self, text: str) -> Decimal | None:
        match = _RIR_PATTERN.match(text)
        if match is None:
            return None

        reps_in_reserve = int(match.group("a") or match.group("b"))
        return RIR_TO_RPE.get(reps_in_reserve)


def effort_for_set(
    *, activity_effort: NormalizedEffort | None, set_effort: NormalizedEffort | None
) -> NormalizedEffort | None:
    """What a single set's effort is, given what the activity stated.

    §14.3: if the effort applies to the activity rather than to a set, the
    system MUST NOT invent the same set-level RPE for every set. Three sets
    logged under one "RPE 8" did not each report an 8 — writing them that way
    would turn one data point into three and make any average built on them
    quietly wrong.

    A pure function rather than a rule inside the command builder, so the
    invariant is testable on its own and WS-8 has one thing to call.
    """
    return set_effort
