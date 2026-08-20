"""Where a stored value came from (§14.4, §26.2, Q51).

Three small vocabularies that the schema and the service both need, kept in the
domain so the database module can import them and never the reverse.

The one worth explaining is `Provenance`. "3x10 60kg, depois mais 2 séries"
states the reps of the last two sets and inherits their load. Storing both as
though the user said them loses the distinction a correction later depends on:
changing a weight the user stated is fixing a typo, and changing one we carried
forward is fixing *our* inference, and only one of those should be confirmed
back to them.
"""

from __future__ import annotations

from enum import StrEnum


class Provenance(StrEnum):
    """Whether a value was stated or carried forward (§14.4)."""

    EXPLICIT = "explicit"
    INHERITED = "inherited"


class SourceRole(StrEnum):
    """How a message relates to a row that exists because of it (§26.2)."""

    CREATED_FROM = "created_from"
    UPDATED_FROM = "updated_from"
    CLARIFIED_BY = "clarified_by"


class ExerciseGroupType(StrEnum):
    """How several exercises were performed together (Q51)."""

    SUPERSET = "superset"
    TRISET = "triset"
    CIRCUIT = "circuit"
    COMPLEX = "complex"
