"""The activity vocabulary (§14.1, §14.2, Q45, Q49).

WS-1 needs two of these enums to type the catalog's columns, so they live here
from the start and WS-2 extends the module with the rest of the activity model.
The direction matters: `infrastructure/postgres` imports from `domain`, never
the reverse, and putting the vocabulary in the schema module would invert that
the first time a validator needed it.
"""

from __future__ import annotations

from enum import StrEnum


class ActivityType(StrEnum):
    """What kind of thing was performed (Q45).

    Declared in full from the MVP because the *schema* has to accommodate all
    of them; how each is validated is WS-2's concern.
    """

    STRENGTH = "strength"
    DISTANCE_ACTIVITY = "distance_activity"
    TIMED_ACTIVITY = "timed_activity"
    MIXED_ACTIVITY = "mixed_activity"
    MOBILITY = "mobility"
    OTHER = "other"


class LoadMode(StrEnum):
    """How a reported load should be read (§14.2, Q49).

    A dumbbell exercise defaults to PER_IMPLEMENT: "20 kg" on a dumbbell curl
    means 20 kg in each hand, and reading it as a total would halve every
    volume calculation built on it later.
    """

    TOTAL = "total"
    PER_SIDE = "per_side"
    PER_IMPLEMENT = "per_implement"
    BODYWEIGHT = "bodyweight"
    BODYWEIGHT_PLUS = "bodyweight_plus"
    BODYWEIGHT_MINUS = "bodyweight_minus"
