"""WS-4: effort normalization, deterministic half (§14.3).

The assertion that matters most in this file is a negative one: an effort
phrase the table does not contain is stored raw with **no** RPE. Guessing that
"foi osso" means 8.5 would put a number into a history that the user never
gave, and every trend built on it afterwards would be partly fiction.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.training.effort import (
    EFFORT_VERSION,
    PT_BR_EFFORT_PHRASES,
    RIR_TO_RPE,
    EffortMethod,
    EffortNormalizer,
    effort_for_set,
)


@pytest.fixture
def normalizer() -> EffortNormalizer:
    return EffortNormalizer()


# --------------------------------------------------------------------------
# Explicit RPE
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["RPE 8", "rpe8", "8 RPE", "rpe 8", "  RPE8  "])
def test_an_explicit_rpe_is_taken_as_given(normalizer: EffortNormalizer, raw: str) -> None:
    effort = normalizer.normalize(raw)

    assert effort is not None
    assert effort.rpe == Decimal(8)
    assert effort.method is EffortMethod.EXPLICIT_RPE
    assert effort.raw == raw


def test_a_fractional_rpe_survives(normalizer: EffortNormalizer) -> None:
    """Half points are how people actually report: 8.5 is not 8 or 9."""
    effort = normalizer.normalize("RPE 8,5")

    assert effort is not None
    assert effort.rpe == Decimal("8.5")


@pytest.mark.parametrize("raw", ["RPE 42", "RPE 0", "rpe -3", "RPE 11"])
def test_an_rpe_outside_the_scale_is_not_a_reading(normalizer: EffortNormalizer, raw: str) -> None:
    """RPE is 1-10. A "42" is a typo or a joke, and storing it would put a
    value into the scale that no analysis can interpret."""
    effort = normalizer.normalize(raw)

    assert effort is not None
    assert effort.rpe is None
    assert effort.method is EffortMethod.UNNORMALIZED
    assert effort.raw == raw


# --------------------------------------------------------------------------
# RIR
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("rir", "expected_rpe"), sorted(RIR_TO_RPE.items()))
def test_the_rir_table_maps_value_by_value(
    normalizer: EffortNormalizer, rir: int, expected_rpe: Decimal
) -> None:
    """Enumerated rather than sampled: the table is a claim about six distinct
    inputs, and a sample would let one of them rot."""
    effort = normalizer.normalize(f"RIR {rir}")

    assert effort is not None
    assert effort.rpe == expected_rpe
    assert effort.method is EffortMethod.RIR_MAPPED


def test_the_rir_table_covers_zero_through_five() -> None:
    assert sorted(RIR_TO_RPE) == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("raw", ["RIR 2", "rir2", "2 RIR", "rir 2"])
def test_rir_is_recognized_in_the_forms_people_type(normalizer: EffortNormalizer, raw: str) -> None:
    effort = normalizer.normalize(raw)

    assert effort is not None
    assert effort.rpe == Decimal(8)


@pytest.mark.parametrize("raw", ["RIR 9", "rir 20"])
def test_an_unmapped_rir_is_not_extrapolated(normalizer: EffortNormalizer, raw: str) -> None:
    """The table stops at five because past that the mapping stops meaning
    anything. Extending it by arithmetic would invent the part nobody agreed on."""
    effort = normalizer.normalize(raw)

    assert effort is not None
    assert effort.rpe is None
    assert effort.method is EffortMethod.UNNORMALIZED


# --------------------------------------------------------------------------
# The curated phrase table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("phrase", "expected_rpe"), sorted(PT_BR_EFFORT_PHRASES.items()))
def test_every_curated_phrase_round_trips(
    normalizer: EffortNormalizer, phrase: str, expected_rpe: Decimal
) -> None:
    effort = normalizer.normalize(phrase)

    assert effort is not None
    assert effort.rpe == expected_rpe
    assert effort.method is EffortMethod.PHRASE_TABLE


@pytest.mark.parametrize(
    ("raw", "expected_rpe"),
    [
        ("Quase Falhei", Decimal("9.5")),
        ("QUASE FALHEI", Decimal("9.5")),
        ("até a falha", Decimal("10")),
        ("Até a Falha", Decimal("10")),
        ("  puxado  ", Decimal("8")),
    ],
)
def test_case_and_accents_do_not_hide_a_phrase(
    normalizer: EffortNormalizer, raw: str, expected_rpe: Decimal
) -> None:
    """A user typing with accents and one typing without mean the same thing,
    and a table that only matches one of them is a table that mostly misses."""
    effort = normalizer.normalize(raw)

    assert effort is not None
    assert effort.rpe == expected_rpe


def test_the_phrase_table_stays_small() -> None:
    """Every entry is a claim about what somebody meant. A large table is a
    large number of claims nobody verified."""
    assert len(PT_BR_EFFORT_PHRASES) <= 15


# --------------------------------------------------------------------------
# What must never be invented
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["foi osso", "sei lá", "deu pra fazer", "tranquilão demais"])
def test_an_unknown_phrase_is_kept_raw_and_left_unnormalized(
    normalizer: EffortNormalizer, raw: str
) -> None:
    """The §14.3 deviation this sprint records: the LLM classifier that would
    read these arrives in Sprint 3. Until then the phrase is preserved and no
    number is produced — a guessed 8.5 is indistinguishable from a reported one
    the moment it is stored."""
    effort = normalizer.normalize(raw)

    assert effort is not None
    assert effort.raw == raw
    assert effort.rpe is None
    assert effort.method is EffortMethod.UNNORMALIZED


def test_no_effort_stated_is_different_from_an_unreadable_one(
    normalizer: EffortNormalizer,
) -> None:
    """Two different facts, and they must stay different rows: one says the
    user said nothing, the other says they said something we could not read."""
    assert normalizer.normalize(None) is None
    assert normalizer.normalize("   ") is None

    unreadable = normalizer.normalize("foi osso")
    assert unreadable is not None
    assert unreadable.method is EffortMethod.UNNORMALIZED


def test_every_outcome_records_its_method_and_version(
    normalizer: EffortNormalizer,
) -> None:
    """§14.3 requires the normalization method and version to be persisted, so
    a stored RPE can be re-read knowing how it was produced."""
    for raw in ("RPE 8", "RIR 2", "puxado", "foi osso"):
        effort = normalizer.normalize(raw)
        assert effort is not None
        assert effort.version == EFFORT_VERSION
        assert isinstance(effort.method, EffortMethod)


def test_a_custom_phrase_table_is_honoured() -> None:
    """Injectable so a later sprint can extend the vocabulary without editing
    the parser — and so a test can prove the table is what decides."""
    normalizer = EffortNormalizer(phrases={"osso": Decimal("9")})

    effort = normalizer.normalize("osso")

    assert effort is not None
    assert effort.rpe == Decimal(9)


# --------------------------------------------------------------------------
# Effort belongs to the thing that stated it (§14.3)
# --------------------------------------------------------------------------


def test_a_set_without_its_own_effort_gets_none(normalizer: EffortNormalizer) -> None:
    """§14.3: if effort applies to the activity, the system MUST NOT invent the
    same set-level RPE for every set. Three sets under one "RPE 8" did not each
    report an 8, and storing them that way would triple a single data point."""
    activity_effort = normalizer.normalize("RPE 8")

    assert effort_for_set(activity_effort=activity_effort, set_effort=None) is None


def test_a_set_that_stated_its_own_effort_keeps_it(normalizer: EffortNormalizer) -> None:
    activity_effort = normalizer.normalize("RPE 7")
    set_effort = normalizer.normalize("RPE 9")

    kept = effort_for_set(activity_effort=activity_effort, set_effort=set_effort)

    assert kept is not None
    assert kept.rpe == Decimal(9)


def test_a_set_effort_survives_when_the_activity_stated_nothing(
    normalizer: EffortNormalizer,
) -> None:
    set_effort = normalizer.normalize("falhei")

    kept = effort_for_set(activity_effort=None, set_effort=set_effort)

    assert kept is not None
    assert kept.rpe == Decimal(10)


@pytest.mark.parametrize("raw", ["RIR -0", "-0 rir", "rir -2"])
def test_a_signed_rir_is_not_a_reading(normalizer: EffortNormalizer, raw: str) -> None:
    """`int("-0")` is zero, so a signed zero would map to RPE 10 — a typo
    silently promoted to a maximal-effort measurement. Reps in reserve are
    never negative, so the sign is refused rather than dropped."""
    effort = normalizer.normalize(raw)

    assert effort is not None
    assert effort.rpe is None
    assert effort.method is EffortMethod.UNNORMALIZED


def test_an_injected_phrase_table_is_normalized_like_the_default() -> None:
    """The table is injectable so a later sprint can extend the vocabulary. If
    callers had to pre-normalize their keys, an entry written the way a person
    would write it — accents and capitals — would silently never match, and the
    extension point would work only for whoever knew the rule."""
    normalizer = EffortNormalizer(phrases={"Até a Falha": Decimal("10"), "OSSO": Decimal("9")})

    for raw in ("ate a falha", "até a falha", "osso", "Osso"):
        effort = normalizer.normalize(raw)
        assert effort is not None, raw
        assert effort.method is EffortMethod.PHRASE_TABLE, raw
