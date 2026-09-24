"""AP-1: domain mapping (time format, rounding, start type compatibility)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.vfsync.vf_client import mapping
from app.vfsync.vf_client.mapping import (
    AEROTOW_AMBIGUOUS,
    READ_MAP,
    WRITE_MAP,
    StartType,
    from_vf_time,
    is_starttype_compatible,
    normalize_callsign,
    to_vf_time,
    tow_time_minutes,
)
from app.vfsync.vf_client.models import VfFlight

UTC = timezone.utc
CEST = timezone(timedelta(hours=2))


# ------------------------------------------------------------------ time

@pytest.mark.parametrize("dt, expected", [
    (datetime(2026, 9, 24, 10, 15, 59, tzinfo=UTC), "2026-09-24 10:15"),
    (datetime(2026, 9, 24, 10, 15, 0, tzinfo=UTC), "2026-09-24 10:15"),
    (datetime(2026, 9, 24, 12, 15, 30, tzinfo=CEST), "2026-09-24 10:15"),   # → UTC
    (datetime(2026, 9, 24, 10, 15, 45), "2026-09-24 10:15"),                # naive = UTC
    (datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC), "2026-01-01 00:00"),
])
def test_to_vf_time(dt, expected):
    assert to_vf_time(dt) == expected


@pytest.mark.parametrize("s", ["", None, "0000-00-00 00:00", "0000-00-00 00:00:00", "  "])
def test_from_vf_time_empty(s):
    assert from_vf_time(s) is None


@pytest.mark.parametrize("s, expected", [
    ("2026-09-24 10:15", datetime(2026, 9, 24, 10, 15, tzinfo=UTC)),
    ("2026-09-24 10:15:42", datetime(2026, 9, 24, 10, 15, 42, tzinfo=UTC)),
    (" 2026-09-24 10:15 ", datetime(2026, 9, 24, 10, 15, tzinfo=UTC)),
])
def test_from_vf_time_parses_utc(s, expected):
    got = from_vf_time(s)
    assert got == expected
    assert got.tzinfo is not None and got.utcoffset() == timedelta(0)


@pytest.mark.parametrize("s", ["24.09.2026 10:15", "2026-09-24T10:15", "garbage"])
def test_from_vf_time_rejects_garbage(s):
    with pytest.raises(ValueError):
        from_vf_time(s)


def test_time_round_trip_truncates_seconds():
    dt = datetime(2026, 9, 24, 10, 15, 59, tzinfo=UTC)
    assert from_vf_time(to_vf_time(dt)) == dt.replace(second=0)


# -------------------------------------------------------------- rounding

@pytest.mark.parametrize("seconds, minutes", [
    (0, 0), (29, 0), (30, 1), (60, 1),
    (5 * 60 + 29, 5), (5 * 60 + 30, 6), (5 * 60 + 31, 6),
    (6 * 60 + 30, 7),   # half-up, not banker's rounding
    (-5, 0),
])
def test_tow_time_minutes_half_up(seconds, minutes):
    assert mapping.TOWTIME_ROUNDING == "half_up"
    assert tow_time_minutes(seconds) == minutes


@pytest.mark.parametrize("rule, seconds, minutes", [
    ("floor", 5 * 60 + 59, 5),
    ("ceil", 5 * 60 + 1, 6),
    ("ceil", 5 * 60, 5),
])
def test_tow_time_minutes_alternative_rules(monkeypatch, rule, seconds, minutes):
    monkeypatch.setattr(mapping, "TOWTIME_ROUNDING", rule)
    assert tow_time_minutes(seconds) == minutes


# ---------------------------------------------------------- compatibility

def test_maps_per_spec():
    assert WRITE_MAP == {StartType.AEROTOW: "F", StartType.WINCH: "W", StartType.SELF: "E"}
    assert StartType.POWERED not in WRITE_MAP
    assert READ_MAP[1] == {StartType.SELF, StartType.POWERED}
    assert READ_MAP[3] == {StartType.AEROTOW}
    assert READ_MAP[5] == {StartType.WINCH}
    assert READ_MAP[7] == set() and READ_MAP[9] == set()


@pytest.mark.parametrize("detected", list(StartType) + [AEROTOW_AMBIGUOUS, None])
@pytest.mark.parametrize("vf_value", [None, "", 0, "0", " "])
def test_empty_vf_starttype_is_always_compatible(detected, vf_value):
    assert is_starttype_compatible(detected, vf_value) is True


@pytest.mark.parametrize("detected, vf_value, expected", [
    # numeric codes (int and numeric string)
    (StartType.AEROTOW, 3, True),
    (StartType.AEROTOW, "3", True),
    (StartType.AEROTOW, 5, False),
    (StartType.WINCH, "5", True),
    (StartType.WINCH, 3, False),
    (StartType.SELF, 1, True),
    (StartType.POWERED, "1", True),
    (StartType.SELF, 3, False),
    (StartType.AEROTOW, 7, False),          # bungee
    (StartType.WINCH, 9, False),            # vehicle
    (StartType.AEROTOW, 42, False),         # unknown code
    # letters (write alphabet echoed back)
    (StartType.AEROTOW, "F", True),
    (StartType.AEROTOW, "f", True),
    (StartType.WINCH, "W", True),
    (StartType.SELF, "E", True),
    (StartType.POWERED, "E", True),
    (StartType.WINCH, "F", False),
    (StartType.AEROTOW, "X", False),        # unknown letter
    # detected values as plain strings
    ("aerotow", 3, True),
    ("winch", "W", True),
    # no / failed classification cannot contradict the pilot's entry
    (StartType.UNKNOWN, 3, True),
    (StartType.UNKNOWN, "F", True),
    (None, 3, True),
    # an ambiguous tow is still a tow
    (AEROTOW_AMBIGUOUS, 3, True),
    (AEROTOW_AMBIGUOUS, "F", True),
    (AEROTOW_AMBIGUOUS, 5, False),
    (AEROTOW_AMBIGUOUS, "W", False),
    ("something_else", 3, False),
])
def test_starttype_compatibility_table(detected, vf_value, expected):
    assert is_starttype_compatible(detected, vf_value) is expected


# ------------------------------------------------------------- callsign

@pytest.mark.parametrize("raw, expected", [
    ("D-1234 ", "D1234"),
    ("d-1234", "D1234"),
    (" D 1234 ", "D1234"),
    ("D-EKPO", "DEKPO"),
    ("", ""),
    (None, ""),
])
def test_normalize_callsign(raw, expected):
    assert normalize_callsign(raw) == expected


# ------------------------------------------------------------- VfFlight

def test_vfflight_tolerates_vf_string_values_and_extras():
    f = VfFlight.model_validate({
        "flid": "4711", "callsign": "D-1234", "departuretime": "2026-09-24 10:15",
        "arrivaltime": "", "starttype": "3", "towheight": "450", "towtime": "",
        "landingcount": "", "towflid": "4712", "unknownfield": "x", "pilotname": None,
    })
    assert f.flid == 4711
    assert f.towheight == 450
    assert f.towtime is None
    assert f.landingcount == 1
    assert f.towflid == 4712
    assert f.starttype == "3"
    assert f.pilotname == ""
    assert f.departure_dt == datetime(2026, 9, 24, 10, 15, tzinfo=UTC)
    assert f.arrival_dt is None
    assert f.raw()["unknownfield"] == "x"


@pytest.mark.parametrize("field, value, empty", [
    ("arrivaltime", "", True),
    ("arrivaltime", "0000-00-00 00:00", True),
    ("arrivaltime", "0000-00-00 00:00:00", True),
    ("arrivaltime", "2026-09-24 10:15", False),
    ("towheight", "", True),
    ("towheight", "0", True),
    ("towheight", "450", False),
    ("starttype", "", True),
    ("starttype", "0", True),
    ("starttype", "3", False),
    ("landingcount", "0", True),
    ("landingcount", "1", False),
    ("comment", "", True),
    ("comment", "x", False),
])
def test_vfflight_is_empty(field, value, empty):
    f = VfFlight.model_validate({"flid": 1, field: value})
    assert f.is_empty(field) is empty


def test_vfflight_is_empty_for_missing_field():
    assert VfFlight(flid=1).is_empty("doesnotexist") is True
