"""WS-3: parsing what people type, and refusing what cannot be read (D4).

The golden table below is the contract with real input. The refusals matter as
much as the conversions: a parser that guesses turns a typo into a set somebody
never performed, and nothing downstream can tell the difference afterwards.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.training.units import (
    Quantity,
    Unit,
    UnitParseError,
    parse_distance,
    parse_duration,
    parse_load,
    to_kilograms,
    to_meters,
    to_seconds,
)


@pytest.mark.parametrize(
    ("raw", "expected_kg"),
    [
        ("80kg", Decimal(80)),
        ("80 kg", Decimal(80)),
        ("80", Decimal(80)),
        ("80.5kg", Decimal("80.5")),
        ("80,5 kg", Decimal("80.5")),
        ("100 quilos", Decimal(100)),
        ("176lb", Decimal("79.832")),
        ("176 lbs", Decimal("79.832")),
        ("0", Decimal(0)),
    ],
)
def test_load_parses_to_kilograms(raw: str, expected_kg: Decimal) -> None:
    assert to_kilograms(parse_load(raw)).quantize(Decimal("0.001")) == expected_kg


@pytest.mark.parametrize(
    ("raw", "expected_m"),
    [
        ("5k", Decimal(5000)),
        ("5km", Decimal(5000)),
        ("5 km", Decimal(5000)),
        ("1500m", Decimal(1500)),
        ("1500 metros", Decimal(1500)),
        ("3mi", Decimal("4828.032")),
        ("10,5km", Decimal(10500)),
    ],
)
def test_distance_parses_to_meters(raw: str, expected_m: Decimal) -> None:
    assert to_meters(parse_distance(raw)) == expected_m


@pytest.mark.parametrize(
    ("raw", "expected_s"),
    [
        ("90s", Decimal(90)),
        ("90 seg", Decimal(90)),
        ("45min", Decimal(2700)),
        ("1:30", Decimal(90)),
        ("1h05", Decimal(3900)),
        ("1min30", Decimal(90)),
        ("2h", Decimal(7200)),
        ("0:45", Decimal(45)),
    ],
)
def test_duration_parses_to_seconds(raw: str, expected_s: Decimal) -> None:
    assert to_seconds(parse_duration(raw)) == expected_s


def test_a_clock_separator_decides_both_halves() -> None:
    """`1h05` is an hour and five *minutes*. Reading the second half as seconds
    would silently shorten every logged run by nearly five minutes."""
    assert to_seconds(parse_duration("1h05")) == Decimal(3900)
    assert to_seconds(parse_duration("1:05")) == Decimal(65)


# --------------------------------------------------------------------------
# What must be refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["5", "10", "0"])
def test_a_bare_number_is_not_a_distance(raw: str) -> None:
    """`5` is five kilometres to a runner and five hundred metres to nobody.
    The system cannot tell, and a wrong distance is indistinguishable from a
    real one once it is stored."""
    with pytest.raises(UnitParseError) as excinfo:
        parse_distance(raw)

    assert "unit" in str(excinfo.value)


@pytest.mark.parametrize("raw", ["5", "45"])
def test_a_bare_number_is_not_a_duration(raw: str) -> None:
    with pytest.raises(UnitParseError):
        parse_duration(raw)


def test_a_bare_number_is_a_load_because_a_gym_has_one_reading() -> None:
    """ "supino 80" means kilograms to everyone who says it. Refusing it would
    make the common case the awkward one, and unlike distance there is no
    second plausible reading."""
    assert parse_load("80") == Quantity(value=Decimal(80), unit=Unit.KILOGRAM)


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "oito quilos", "80kgs extra", "muito peso", "80 kg 90 kg", "-80kg"],
)
def test_unreadable_load_raises_rather_than_defaulting(raw: str) -> None:
    with pytest.raises(UnitParseError):
        parse_load(raw)


@pytest.mark.parametrize("raw", ["", "cinco km", "5 quarteirões", "km"])
def test_unreadable_distance_raises(raw: str) -> None:
    with pytest.raises(UnitParseError):
        parse_distance(raw)


@pytest.mark.parametrize("raw", ["", "uma hora", "90:90", "min"])
def test_unreadable_duration_raises(raw: str) -> None:
    with pytest.raises(UnitParseError):
        parse_duration(raw)


def test_the_error_says_what_was_expected() -> None:
    """The message reaches a developer reading a log, and "invalid input" tells
    them nothing about which of three parsers rejected it."""
    with pytest.raises(UnitParseError) as excinfo:
        parse_duration("5")

    assert excinfo.value.raw == "5"
    assert "duration" in excinfo.value.expected


# --------------------------------------------------------------------------
# Precision
# --------------------------------------------------------------------------


def test_conversions_stay_decimal() -> None:
    """`0.1 + 0.2` inside a volume total produces a number the user can see and
    nobody can reproduce."""
    assert isinstance(to_kilograms(parse_load("176lb")), Decimal)
    assert isinstance(to_meters(parse_distance("3mi")), Decimal)
    assert isinstance(to_seconds(parse_duration("1:30")), Decimal)


def test_decimal_arithmetic_does_not_drift() -> None:
    total = sum((to_kilograms(parse_load("0.1kg")) for _ in range(10)), start=Decimal(0))

    assert total == Decimal("1.0")


def test_a_mile_is_exact() -> None:
    """1609.344 m is the definition, not an approximation, and rounding it here
    would make every imperial distance slightly wrong forever."""
    assert to_meters(parse_distance("1mi")) == Decimal("1609.344")


def test_converting_the_wrong_dimension_is_an_error() -> None:
    with pytest.raises(UnitParseError):
        to_kilograms(Quantity(value=Decimal(5), unit=Unit.KILOMETER))
