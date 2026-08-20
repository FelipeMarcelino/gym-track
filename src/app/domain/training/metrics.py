"""Derived metrics as versioned pure functions (Q52, DEC-001).

Volume, pace, speed and estimated 1RM are code, not model output, and every
stored value carries the version of the code that produced it. That is what
makes a change to a formula a *new metric* rather than a silent rewrite of
history: last month's volume stays comparable to itself because the row says
which arithmetic produced it.

Two rules run through all of it:

* **Insufficient input returns `None`, never zero.** A zero pace is a claim
  the system is making; `None` is the truth. §19's trend analysis would average
  a fabricated zero without noticing it was never a real measurement.
* **A metric outside its useful range is not produced.** Epley past twelve
  repetitions has more error than signal, and a fabricated number stored with a
  version is worse than no number, because the version makes it look checked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from app.domain.training.activities import ActivityDraft, LoadMode

VOLUME_VERSION: Final = "volume.load_x_reps.v1"
PACE_VERSION: Final = "pace.seconds_per_km.v1"
SPEED_VERSION: Final = "speed.meters_per_second.v1"
ONE_RM_VERSION: Final = "1rm.epley.v1"

VOLUME = "volume"
PACE = "pace"
SPEED = "speed"
ONE_RM = "estimated_one_rm"

METRIC_VERSIONS: Final[Mapping[str, str]] = {
    VOLUME: VOLUME_VERSION,
    PACE: PACE_VERSION,
    SPEED: SPEED_VERSION,
    ONE_RM: ONE_RM_VERSION,
}

#: Epley's formula is fitted to sets people actually do. Past this, the estimate
#: says more about the formula than about the lifter.
MAX_REPS_FOR_ONE_RM: Final = 12

#: Three decimals everywhere: enough to keep pounds-to-kilograms honest, few
#: enough that two runs of the same arithmetic produce the same string.
_PRECISION: Final = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class DerivedMetric:
    name: str
    value: Decimal
    unit: str
    version: str


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_PRECISION)


def volume(
    *,
    load_kg: Decimal | None,
    repetitions: int | None,
    load_mode: LoadMode | None,
    implements: int = 2,
) -> DerivedMetric | None:
    """Total load moved, in kilograms.

    The load mode is what makes this arithmetic rather than a guess (Q49): a
    dumbbell press at 20 kg moved 400 kg over ten reps, not 200, and reading
    the catalog's `PER_IMPLEMENT` is the difference.

    A bodyweight set with no external load yields `None`. Sprint 2 does not
    know the user's body weight, and §14.2 is explicit that it must not need
    to — inventing 70 kg would put a number in the history that nobody measured.
    """
    if load_kg is None or repetitions is None or repetitions <= 0:
        return None
    if load_mode is None:
        # Without it, 20 kg on a dumbbell curl and 20 kg on a barbell curl are
        # the same number, and one of them is half the truth. Defaulting to
        # TOTAL would undercount every implement exercise whose caller forgot
        # to say -- silently, which is the part that matters.
        return None
    if load_mode is LoadMode.BODYWEIGHT:
        return None
    if load_mode is LoadMode.BODYWEIGHT_MINUS:
        # The load is *assistance*, not weight moved. Counting it would make a
        # user who needed more help look like they trained more.
        return None

    multiplier = Decimal(1)
    if load_mode is LoadMode.PER_IMPLEMENT:
        multiplier = Decimal(implements)
    elif load_mode is LoadMode.PER_SIDE:
        multiplier = Decimal(2)

    total = load_kg * multiplier * Decimal(repetitions)
    return DerivedMetric(name=VOLUME, value=_quantize(total), unit="kg", version=VOLUME_VERSION)


def pace(*, distance_m: Decimal | None, duration_s: Decimal | None) -> DerivedMetric | None:
    """Seconds per kilometre. `None` unless both inputs exist and are positive."""
    if distance_m is None or duration_s is None:
        return None
    if distance_m <= 0 or duration_s <= 0:
        return None

    seconds_per_km = duration_s / (distance_m / Decimal(1000))
    return DerivedMetric(
        name=PACE, value=_quantize(seconds_per_km), unit="s/km", version=PACE_VERSION
    )


def speed(*, distance_m: Decimal | None, duration_s: Decimal | None) -> DerivedMetric | None:
    """Metres per second. Same insufficiency rule as pace."""
    if distance_m is None or duration_s is None:
        return None
    if distance_m <= 0 or duration_s <= 0:
        return None

    return DerivedMetric(
        name=SPEED,
        value=_quantize(distance_m / duration_s),
        unit="m/s",
        version=SPEED_VERSION,
    )


#: Load modes whose number is the weight actually lifted. The bodyweight modes
#: are absent because the real maximum includes a body weight this sprint does
#: not know, and BODYWEIGHT_MINUS is worse than unknown: its load is
#: assistance, so more help would read as a higher maximum.
_ONE_RM_LOAD_MODES: Final = (LoadMode.TOTAL, LoadMode.PER_SIDE, LoadMode.PER_IMPLEMENT)


def estimated_one_rm(
    *, load_kg: Decimal | None, repetitions: int | None, load_mode: LoadMode | None
) -> DerivedMetric | None:
    """Epley (D5): `1RM = load * (1 + reps / 30)`.

    Returns `None` past twelve repetitions rather than an estimate nobody
    should act on, and `None` for every bodyweight mode: those estimates would
    be fabricated numbers wearing a version string, which is worse than no
    number because the version makes them look checked.
    """
    if load_kg is None or repetitions is None:
        return None
    if load_mode not in _ONE_RM_LOAD_MODES:
        return None
    if repetitions <= 0 or repetitions > MAX_REPS_FOR_ONE_RM:
        return None
    if load_kg <= 0:
        return None

    estimate = load_kg * (Decimal(1) + Decimal(repetitions) / Decimal(30))
    return DerivedMetric(name=ONE_RM, value=_quantize(estimate), unit="kg", version=ONE_RM_VERSION)


def _volume_v1(inputs: Mapping[str, Any]) -> DerivedMetric | None:
    return volume(
        load_kg=Decimal(str(inputs["load_kg"])),
        repetitions=int(inputs["repetitions"]),
        load_mode=LoadMode(inputs["load_mode"]),
    )


def _one_rm_epley_v1(inputs: Mapping[str, Any]) -> DerivedMetric | None:
    return estimated_one_rm(
        load_kg=Decimal(str(inputs["load_kg"])),
        repetitions=int(inputs["repetitions"]),
        load_mode=LoadMode(inputs.get("load_mode", LoadMode.TOTAL.value)),
    )


def _pace_v1(inputs: Mapping[str, Any]) -> DerivedMetric | None:
    return pace(
        distance_m=Decimal(str(inputs["distance_m"])),
        duration_s=Decimal(str(inputs["duration_s"])),
    )


def _speed_v1(inputs: Mapping[str, Any]) -> DerivedMetric | None:
    return speed(
        distance_m=Decimal(str(inputs["distance_m"])),
        duration_s=Decimal(str(inputs["duration_s"])),
    )


#: Every version this codebase can still compute, current or not.
#:
#: Separate from `METRIC_VERSIONS` on purpose. That mapping says which version a
#: *new* row gets; this one says which versions an *existing* row can be
#: recomputed from. When a formula is bumped to v2, v1 stays here — otherwise
#: the only way to keep the test suite green would be deleting v1's frozen
#: cases, and the historical freeze would exist exactly until the first time it
#: mattered.
SUPPORTED_METRIC_VERSIONS: Final[
    Mapping[str, Callable[[Mapping[str, Any]], DerivedMetric | None]]
] = {
    VOLUME_VERSION: _volume_v1,
    ONE_RM_VERSION: _one_rm_epley_v1,
    PACE_VERSION: _pace_v1,
    SPEED_VERSION: _speed_v1,
}


class UnknownMetricVersionError(LookupError):
    def __init__(self, version: str) -> None:
        super().__init__(f"no implementation is registered for metric version {version!r}")
        self.version = version


def compute_by_version(version: str, inputs: Mapping[str, Any]) -> DerivedMetric | None:
    """Recompute a stored value from its version and its inputs.

    This is what a version on a row is *for*: the number can be produced again
    without knowing which formula happened to be current when it was written.
    """
    try:
        implementation = SUPPORTED_METRIC_VERSIONS[version]
    except KeyError as error:
        raise UnknownMetricVersionError(version) from error
    return implementation(inputs)


def derive_all(draft: ActivityDraft) -> tuple[DerivedMetric, ...]:
    """Every metric this draft supports, skipping the ones it cannot support.

    Order is stable so that a persisted set's metrics are written the same way
    twice, which is what makes two runs of the seed-and-log path comparable.
    """
    candidates = (
        volume(
            load_kg=draft.load_kg,
            repetitions=draft.repetitions,
            load_mode=draft.load_mode,
        ),
        estimated_one_rm(
            load_kg=draft.load_kg,
            repetitions=draft.repetitions,
            load_mode=draft.load_mode,
        ),
        pace(distance_m=draft.distance_m, duration_s=draft.duration_s),
        speed(distance_m=draft.distance_m, duration_s=draft.duration_s),
    )
    return tuple(metric for metric in candidates if metric is not None)
