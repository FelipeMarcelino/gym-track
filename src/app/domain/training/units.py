"""Parsing what people type into SI (§14.1, D4).

Storage is kilograms, meters and seconds, in `Decimal`. Display conversion is a
presentation concern; the domain has one representation so that two sets logged
in different units are comparable without anybody remembering to convert.

`Decimal` rather than `float` is not fussiness: a volume total is summed over
sets and compared against last week, and `0.1 + 0.2` inside that produces a
number a user can see and nobody can reproduce.

**A bare number is kilograms for load, and an error for distance and duration.**
`5` could be five kilometres or five minutes, and this sprint exists precisely
to stop the system from guessing. Load is the exception because "supino 80" has
exactly one reading in a gym, and refusing it would make the common case the
awkward one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final


class Unit(StrEnum):
    KILOGRAM = "kg"
    POUND = "lb"
    METER = "m"
    KILOMETER = "km"
    MILE = "mi"
    SECOND = "s"
    MINUTE = "min"
    HOUR = "h"


@dataclass(frozen=True, slots=True)
class Quantity:
    value: Decimal
    unit: Unit


class UnitParseError(ValueError):
    """The text is not a quantity.

    Raising rather than returning a fallback is the entire point: a default
    value here becomes a set in someone's history that they never performed.
    """

    def __init__(self, raw: str, expected: str) -> None:
        super().__init__(f"{raw!r} is not {expected}")
        self.raw = raw
        self.expected = expected


POUNDS_PER_KILOGRAM: Final = Decimal("2.20462262")
METERS_PER_MILE: Final = Decimal("1609.344")
METERS_PER_KILOMETER: Final = Decimal(1000)
SECONDS_PER_MINUTE: Final = Decimal(60)
SECONDS_PER_HOUR: Final = Decimal(3600)

_NUMBER = r"(?P<value>\d+(?:[.,]\d+)?)"

_LOAD_UNITS: Final[dict[str, Unit]] = {
    "": Unit.KILOGRAM,
    "kg": Unit.KILOGRAM,
    "kgs": Unit.KILOGRAM,
    "quilos": Unit.KILOGRAM,
    "quilo": Unit.KILOGRAM,
    "lb": Unit.POUND,
    "lbs": Unit.POUND,
    "libras": Unit.POUND,
}
_DISTANCE_UNITS: Final[dict[str, Unit]] = {
    "m": Unit.METER,
    "metros": Unit.METER,
    "km": Unit.KILOMETER,
    "k": Unit.KILOMETER,
    "quilometros": Unit.KILOMETER,
    "mi": Unit.MILE,
    "milhas": Unit.MILE,
}
_DURATION_UNITS: Final[dict[str, Unit]] = {
    "s": Unit.SECOND,
    "seg": Unit.SECOND,
    "segundos": Unit.SECOND,
    "min": Unit.MINUTE,
    "m": Unit.MINUTE,
    "minutos": Unit.MINUTE,
    "h": Unit.HOUR,
    "hora": Unit.HOUR,
    "horas": Unit.HOUR,
}

_LOAD_PATTERN = re.compile(rf"^{_NUMBER}\s*(?P<unit>[a-zç]*)$", re.IGNORECASE)
_DISTANCE_PATTERN = re.compile(rf"^{_NUMBER}\s*(?P<unit>[a-z]+)$", re.IGNORECASE)
_DURATION_PATTERN = re.compile(rf"^{_NUMBER}\s*(?P<unit>[a-z]+)$", re.IGNORECASE)
#: `1:30` and `1h05` and `1min30`: a pair separated by a unit or a colon.
_CLOCK_PATTERN = re.compile(
    r"^(?P<first>\d+)\s*(?P<separator>:|h|min|m)\s*(?P<second>\d+)?$", re.IGNORECASE
)


def _decimal(raw: str) -> Decimal:
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation as error:  # pragma: no cover - the regex already filtered
        raise UnitParseError(raw, "a number") from error


def parse_load(raw: str) -> Quantity:
    """`80kg`, `80 kg`, `80`, `176lb`. A bare number means kilograms."""
    text = raw.strip().casefold()
    match = _LOAD_PATTERN.match(text)
    if match is None:
        raise UnitParseError(raw, "a load such as '80kg' or '176lb'")

    unit = _LOAD_UNITS.get(match.group("unit"))
    if unit is None:
        raise UnitParseError(raw, "a load in kg or lb")

    return Quantity(value=_decimal(match.group("value")), unit=unit)


def parse_distance(raw: str) -> Quantity:
    """`5km`, `5k`, `1500m`, `3mi`. A bare number is refused.

    `5` is five kilometres to a runner and five hundred metres to nobody, but
    the system cannot tell which one the user meant — and a wrong distance is
    indistinguishable from a real one once it is stored.
    """
    text = raw.strip().casefold()
    match = _DISTANCE_PATTERN.match(text)
    if match is None:
        raise UnitParseError(raw, "a distance with its unit, such as '5km' or '1500m'")

    unit = _DISTANCE_UNITS.get(match.group("unit"))
    if unit is None:
        raise UnitParseError(raw, "a distance in m, km or mi")

    return Quantity(value=_decimal(match.group("value")), unit=unit)


def parse_duration(raw: str) -> Quantity:
    """`90s`, `45min`, `1:30`, `1h05`, `1min30`. A bare number is refused."""
    text = raw.strip().casefold()

    clock = _CLOCK_PATTERN.match(text)
    if clock is not None:
        return Quantity(value=_clock_to_seconds(clock, raw), unit=Unit.SECOND)

    match = _DURATION_PATTERN.match(text)
    if match is None:
        raise UnitParseError(raw, "a duration with its unit, such as '90s' or '1:30'")

    unit = _DURATION_UNITS.get(match.group("unit"))
    if unit is None:
        raise UnitParseError(raw, "a duration in s, min or h")

    return Quantity(value=_decimal(match.group("value")), unit=unit)


def _clock_to_seconds(match: re.Match[str], raw: str) -> Decimal:
    """`1:30` is a minute and a half; `1h05` is an hour and five minutes.

    The separator decides the units of both halves, so `1h05` cannot be read as
    an hour and five seconds — which is the reading that would silently shorten
    every logged run.
    """
    separator = match.group("separator")
    first = Decimal(match.group("first"))
    second_raw = match.group("second")
    second = Decimal(second_raw) if second_raw else Decimal(0)

    if separator == "h":
        return first * SECONDS_PER_HOUR + second * SECONDS_PER_MINUTE
    if separator in (":", "min", "m"):
        if second >= SECONDS_PER_MINUTE and separator == ":":
            raise UnitParseError(raw, "a duration whose seconds are below 60")
        return first * SECONDS_PER_MINUTE + second
    raise UnitParseError(raw, "a duration such as '1:30' or '1h05'")  # pragma: no cover


def to_kilograms(quantity: Quantity) -> Decimal:
    match quantity.unit:
        case Unit.KILOGRAM:
            return quantity.value
        case Unit.POUND:
            return quantity.value / POUNDS_PER_KILOGRAM
        case _:
            raise UnitParseError(str(quantity.unit), "a mass unit")


def to_meters(quantity: Quantity) -> Decimal:
    match quantity.unit:
        case Unit.METER:
            return quantity.value
        case Unit.KILOMETER:
            return quantity.value * METERS_PER_KILOMETER
        case Unit.MILE:
            return quantity.value * METERS_PER_MILE
        case _:
            raise UnitParseError(str(quantity.unit), "a distance unit")


def to_seconds(quantity: Quantity) -> Decimal:
    match quantity.unit:
        case Unit.SECOND:
            return quantity.value
        case Unit.MINUTE:
            return quantity.value * SECONDS_PER_MINUTE
        case Unit.HOUR:
            return quantity.value * SECONDS_PER_HOUR
        case _:
            raise UnitParseError(str(quantity.unit), "a duration unit")
