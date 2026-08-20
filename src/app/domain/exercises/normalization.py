"""Turning what somebody typed into something matchable (§16).

Lives in the domain because two different layers need exactly the same answer:
the catalog loader stores this form so a lookup is an index seek, and the
resolver computes it on the way in. If they ever disagreed, an alias would be
stored in one shape and searched for in another, and the resolution would fail
for reasons nobody could see in either file.

WS-4's effort phrases use it too: "Quase Falhei" and "quase falhei" are the
same claim about how hard a set was.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")


def normalize_for_match(text: str) -> str:
    """Casefold, strip accents, drop punctuation, collapse whitespace.

    Accents go because half of Brazilian typing omits them and the other half
    does not, and both halves mean the same exercise.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_punctuation = _PUNCTUATION.sub(" ", without_accents)
    return _WHITESPACE.sub(" ", without_punctuation).strip().casefold()
