"""WS-3: derived metrics (Q52).

The recurring assertion is about absence. Every function returns `None` when it
cannot honestly compute something, and each of these tests exists because
returning `0` instead would put a fabricated measurement into a history that
§19's trend analysis will average without noticing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.training.activities import ActivityDraft, ActivityType, LoadMode
from app.domain.training.metrics import (
    METRIC_VERSIONS,
    ONE_RM_VERSION,
    VOLUME_VERSION,
    derive_all,
    estimated_one_rm,
    pace,
    speed,
    volume,
)

# --------------------------------------------------------------------------
# Volume (Q49)
# --------------------------------------------------------------------------


def test_total_load_multiplies_load_by_reps() -> None:
    metric = volume(load_kg=Decimal(80), repetitions=10, load_mode=LoadMode.TOTAL)

    assert metric is not None
    assert metric.value == Decimal("800.000")
    assert metric.unit == "kg"
    assert metric.version == VOLUME_VERSION


def test_per_implement_load_counts_both_hands() -> None:
    """Q49: a dumbbell press at 20 kg moved 400 kg over ten reps, not 200.
    Reading the catalog's load mode is what makes that arithmetic."""
    metric = volume(load_kg=Decimal(20), repetitions=10, load_mode=LoadMode.PER_IMPLEMENT)

    assert metric is not None
    assert metric.value == Decimal("400.000")


def test_per_side_load_doubles() -> None:
    metric = volume(load_kg=Decimal(50), repetitions=8, load_mode=LoadMode.PER_SIDE)

    assert metric is not None
    assert metric.value == Decimal("800.000")


def test_a_bodyweight_set_has_no_volume_this_sprint() -> None:
    """Sprint 2 does not know the user's body weight, and §14.2 says it must
    not need to. Inventing 70 kg would put a number nobody measured into the
    history."""
    assert volume(load_kg=Decimal(0), repetitions=20, load_mode=LoadMode.BODYWEIGHT) is None


def test_bodyweight_plus_external_load_still_counts_the_extra() -> None:
    """A weighted pull-up moved something real, even if the body is unknown."""
    metric = volume(load_kg=Decimal(10), repetitions=5, load_mode=LoadMode.BODYWEIGHT_PLUS)

    assert metric is not None
    assert metric.value == Decimal("50.000")


@pytest.mark.parametrize(
    ("load_kg", "repetitions"),
    [(None, 10), (Decimal(80), None), (Decimal(80), 0), (None, None)],
)
def test_volume_is_absent_rather_than_zero(
    load_kg: Decimal | None, repetitions: int | None
) -> None:
    assert volume(load_kg=load_kg, repetitions=repetitions, load_mode=LoadMode.TOTAL) is None


# --------------------------------------------------------------------------
# Pace and speed (Q47, Q52)
# --------------------------------------------------------------------------


def test_pace_is_seconds_per_kilometre() -> None:
    metric = pace(distance_m=Decimal(5000), duration_s=Decimal(1500))

    assert metric is not None
    assert metric.value == Decimal("300.000")
    assert metric.unit == "s/km"


def test_speed_is_metres_per_second() -> None:
    metric = speed(distance_m=Decimal(5000), duration_s=Decimal(1500))

    assert metric is not None
    assert metric.value == Decimal("3.333")


@pytest.mark.parametrize(
    ("distance_m", "duration_s"),
    [
        (None, Decimal(1500)),
        (Decimal(5000), None),
        (None, None),
        (Decimal(0), Decimal(1500)),
        (Decimal(5000), Decimal(0)),
    ],
)
def test_pace_and_speed_are_absent_rather_than_zero(
    distance_m: Decimal | None, duration_s: Decimal | None
) -> None:
    """A zero pace is a claim the system is making. `None` is the truth, and
    the difference survives into every average built on top of it."""
    assert pace(distance_m=distance_m, duration_s=duration_s) is None
    assert speed(distance_m=distance_m, duration_s=duration_s) is None


# --------------------------------------------------------------------------
# Estimated 1RM (D5)
# --------------------------------------------------------------------------


def test_epley_matches_the_hand_computed_value() -> None:
    """100 kg for 5 reps: 100 * (1 + 5/30) = 116.667."""
    metric = estimated_one_rm(load_kg=Decimal(100), repetitions=5, load_mode=LoadMode.TOTAL)

    assert metric is not None
    assert metric.value == Decimal("116.667")
    assert metric.version == ONE_RM_VERSION


def test_epley_adds_its_increment_even_at_one_repetition() -> None:
    """Not the load itself: Epley is `load * (1 + reps/30)`, so a single rep
    still adds 1/30. Worth pinning because the intuition says otherwise."""
    metric = estimated_one_rm(load_kg=Decimal(120), repetitions=1, load_mode=LoadMode.TOTAL)

    assert metric is not None
    assert metric.value == Decimal("124.000")


@pytest.mark.parametrize("repetitions", [13, 20, 100])
def test_no_estimate_past_twelve_repetitions(repetitions: int) -> None:
    """Epley's error past twelve is larger than the signal, and a fabricated
    number stored *with a version* is worse than no number — the version makes
    it look checked."""
    assert (
        estimated_one_rm(load_kg=Decimal(60), repetitions=repetitions, load_mode=LoadMode.TOTAL)
        is None
    )


@pytest.mark.parametrize(
    ("load_kg", "repetitions"),
    [(None, 5), (Decimal(100), None), (Decimal(100), 0), (Decimal(0), 5)],
)
def test_one_rm_is_absent_when_it_cannot_be_computed(
    load_kg: Decimal | None, repetitions: int | None
) -> None:
    assert (
        estimated_one_rm(load_kg=load_kg, repetitions=repetitions, load_mode=LoadMode.TOTAL) is None
    )


# --------------------------------------------------------------------------
# derive_all
# --------------------------------------------------------------------------


def test_a_strength_draft_derives_volume_and_one_rm() -> None:
    metrics = derive_all(
        ActivityDraft(
            activity_type=ActivityType.STRENGTH,
            repetitions=10,
            load_kg=Decimal(80),
            load_mode=LoadMode.TOTAL,
        )
    )

    assert [metric.name for metric in metrics] == ["volume", "estimated_one_rm"]


def test_a_run_derives_pace_and_speed() -> None:
    metrics = derive_all(
        ActivityDraft(
            activity_type=ActivityType.DISTANCE_ACTIVITY,
            distance_m=Decimal(5000),
            duration_s=Decimal(1500),
        )
    )

    assert [metric.name for metric in metrics] == ["pace", "speed"]


def test_a_draft_with_nothing_derivable_yields_nothing() -> None:
    metrics = derive_all(ActivityDraft(activity_type=ActivityType.STRENGTH, repetitions=10))

    assert metrics == ()


def test_every_derived_metric_carries_a_known_version() -> None:
    """Q52: a stored value without the version that produced it cannot be
    compared against next month's."""
    drafts = [
        ActivityDraft(
            activity_type=ActivityType.STRENGTH,
            repetitions=5,
            load_kg=Decimal(100),
            load_mode=LoadMode.PER_IMPLEMENT,
        ),
        ActivityDraft(
            activity_type=ActivityType.DISTANCE_ACTIVITY,
            distance_m=Decimal(10_000),
            duration_s=Decimal(3000),
        ),
    ]

    for draft in drafts:
        for metric in derive_all(draft):
            assert METRIC_VERSIONS[metric.name] == metric.version


def test_derivation_is_stable_across_runs() -> None:
    """Two runs of the same arithmetic must write the same rows, or a replay
    produces a diff nobody caused."""
    draft = ActivityDraft(
        activity_type=ActivityType.STRENGTH,
        repetitions=8,
        load_kg=Decimal("82.5"),
        load_mode=LoadMode.TOTAL,
    )

    assert derive_all(draft) == derive_all(draft)


# --------------------------------------------------------------------------
# What the load mode changes
# --------------------------------------------------------------------------


def test_volume_needs_a_load_mode_to_be_arithmetic() -> None:
    """Without one, 20 kg on a dumbbell curl and 20 kg on a barbell curl are
    the same number — and one of them is half the truth. Defaulting to TOTAL
    would undercount every implement exercise whose caller forgot to say."""
    assert volume(load_kg=Decimal(20), repetitions=10, load_mode=None) is None


def test_assistance_is_not_volume() -> None:
    """BODYWEIGHT_MINUS carries assistance, not weight lifted. Counting it
    would make a user who needed *more* help look like they trained more."""
    assert volume(load_kg=Decimal(30), repetitions=8, load_mode=LoadMode.BODYWEIGHT_MINUS) is None


@pytest.mark.parametrize(
    "load_mode",
    [LoadMode.BODYWEIGHT, LoadMode.BODYWEIGHT_PLUS, LoadMode.BODYWEIGHT_MINUS],
)
def test_no_one_rep_maximum_for_bodyweight_modes(load_mode: LoadMode) -> None:
    """The real maximum includes a body weight this sprint does not know, so
    every one of these estimates would be a fabricated number wearing a version
    string. Assistance is the worst of the three: more help would read as a
    higher maximum."""
    assert estimated_one_rm(load_kg=Decimal(20), repetitions=5, load_mode=load_mode) is None


def test_derive_all_skips_what_the_load_mode_makes_dishonest() -> None:
    metrics = derive_all(
        ActivityDraft(
            activity_type=ActivityType.STRENGTH,
            repetitions=8,
            load_kg=Decimal(30),
            load_mode=LoadMode.BODYWEIGHT_MINUS,
        )
    )

    assert metrics == ()


def test_derive_all_still_counts_added_weight() -> None:
    """A weighted pull-up moved something real: the volume is honest even
    though the maximum is not computable."""
    metrics = derive_all(
        ActivityDraft(
            activity_type=ActivityType.STRENGTH,
            repetitions=5,
            load_kg=Decimal(10),
            load_mode=LoadMode.BODYWEIGHT_PLUS,
        )
    )

    assert [metric.name for metric in metrics] == ["volume"]
