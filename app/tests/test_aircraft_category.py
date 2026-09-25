"""Aircraft category normalisation (OGN beacon type, tenant / registry strings)
and the ignored-category config list."""

import pytest

from app.tracking.aircraft_category import (
    AircraftCategory as C,
    category_for_beacon,
    category_from_ddb,
    category_from_ogn_type,
    parse_category_list,
)


# ---------------------------------------------------------------------------
# OGN beacon type (bits 5..2 of the id byte)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code, expected", [
    (0, C.UNKNOWN),
    (1, C.GLIDER),
    (2, C.TOW_PLANE),
    (3, C.HELICOPTER),
    (4, C.PARACHUTE_DROP),
    (5, C.POWERED),  # drop plane stays tracked (takes off/lands at glider fields)
    (6, C.PARAGLIDER_HANGGLIDER),
    (7, C.PARAGLIDER_HANGGLIDER),
    (8, C.POWERED),
    (9, C.JET),
    (10, C.UNKNOWN),        # UFO
    (11, C.BALLOON_AIRSHIP),
    (12, C.BALLOON_AIRSHIP),
    (13, C.UAV),
    (14, C.UNKNOWN),        # reserved
    (15, C.STATIC),
    (16, C.UNKNOWN),
    (-1, C.UNKNOWN),
    (None, C.UNKNOWN),
])
def test_category_from_ogn_type(code, expected):
    assert category_from_ogn_type(code) is expected


# ---------------------------------------------------------------------------
# Tenant / registry strings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("glider", C.GLIDER),
    ("Glider", C.GLIDER),
    ("tow_plane", C.TOW_PLANE),
    ("towplane", C.TOW_PLANE),
    ("motor_glider", C.MOTOR_GLIDER),
    ("tmg", C.MOTOR_GLIDER),
    ("motorglider_sl", C.MOTOR_GLIDER),
    ("helicopter", C.HELICOPTER),
    ("Hubschrauber", C.HELICOPTER),
    ("powered", C.POWERED),
    ("ultralight", C.ULTRALIGHT),
    ("UL", C.ULTRALIGHT),
    ("jet", C.JET),
    ("balloon", C.BALLOON_AIRSHIP),
    ("airship", C.BALLOON_AIRSHIP),
    ("uav", C.UAV),
    ("drone", C.UAV),
    ("paraglider", C.PARAGLIDER_HANGGLIDER),
    ("hang-glider", C.PARAGLIDER_HANGGLIDER),
    ("parachute", C.PARACHUTE_DROP),
    ("static", C.STATIC),
    ("3", C.HELICOPTER),          # numeric OGN code as string
    ("", C.UNKNOWN),
    (None, C.UNKNOWN),
    ("   ", C.UNKNOWN),
    ("spaceship", C.UNKNOWN),
])
def test_category_from_ddb_strings(value, expected):
    assert category_from_ddb(value) is expected


@pytest.mark.parametrize("letter", ["F", "I", "O"])
def test_ddb_address_type_letters_are_not_a_category(letter):
    """The DDB DEVICE_TYPE (FLARM / ICAO / OGN tracker) is an address type;
    'I' must not become 'tow plane' (legacy _parse_device_type coding)."""
    assert category_from_ddb(letter) is C.UNKNOWN


def test_every_category_value_round_trips_through_ddb_parser():
    for cat in C:
        assert category_from_ddb(cat.value) is cat


# ---------------------------------------------------------------------------
# Resolution order for a beacon
# ---------------------------------------------------------------------------

def test_known_category_wins_over_beacon_type():
    assert category_for_beacon(C.GLIDER, 3) is C.GLIDER


def test_unknown_known_category_falls_back_to_beacon_type():
    assert category_for_beacon(C.UNKNOWN, 3) is C.HELICOPTER
    assert category_for_beacon(None, 3) is C.HELICOPTER
    assert category_for_beacon(None, 0) is C.UNKNOWN


# ---------------------------------------------------------------------------
# Config list
# ---------------------------------------------------------------------------

def test_parse_category_list_default_value():
    parsed = parse_category_list("helicopter,balloon_airship,uav,static,parachute_drop")
    assert parsed == frozenset({C.HELICOPTER, C.BALLOON_AIRSHIP, C.UAV, C.STATIC,
                                C.PARACHUTE_DROP})


def test_parse_category_list_is_lenient_about_case_and_blanks():
    assert parse_category_list(" Helicopter , UAV,, ") == frozenset({C.HELICOPTER, C.UAV})
    assert parse_category_list("TOW_PLANE") == frozenset({C.TOW_PLANE})


def test_parse_category_list_empty_means_no_filter():
    assert parse_category_list("") == frozenset()
    assert parse_category_list(None) == frozenset()


def test_parse_category_list_rejects_unknown_names():
    with pytest.raises(ValueError) as exc:
        parse_category_list("helicopter,spaceship")
    assert "spaceship" in str(exc.value)
    assert "glider" in str(exc.value)          # lists the valid names


def test_parse_category_list_rejects_unknown_category_itself():
    with pytest.raises(ValueError):
        parse_category_list("unknown")
