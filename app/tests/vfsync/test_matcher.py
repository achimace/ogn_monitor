"""Matcher decision table (Konzept 4.2, R-06)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.vfsync.matcher import match_session
from app.vfsync.models import Session
from app.vfsync.vf_client.models import VfFlight

AF = uuid4()
T = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)
DEP = {"departuretime"}
BUNDLE = {"arrivaltime", "towheight", "towtime", "landingcount"}


def _session(**kw) -> Session:
    base = dict(airfield_id=AF, flarm_id="DDA5BA", registration="D-1234",
                takeoff_ts=T, start_type_detected="aerotow")
    base.update(kw)
    return Session(**base)


def _flight(flid: int, callsign="D-1234", **kw) -> VfFlight:
    return VfFlight(flid=flid, callsign=callsign, **kw)


def test_single_open_flight_matches():
    d = match_session(_session(), [_flight(1)], DEP)
    assert d.kind == "matched" and d.flid == 1 and d.reason == "single_open_flight"


def test_callsign_normalisation():
    d = match_session(_session(registration="d1234"), [_flight(7, callsign="D-1234 ")], DEP)
    assert d.kind == "matched" and d.flid == 7


def test_other_callsigns_ignored():
    d = match_session(_session(), [_flight(1, callsign="D-9999")], DEP)
    assert d.kind == "awaiting_match" and d.reason == "no_candidate"


def test_no_registration_waits():
    d = match_session(_session(registration=None), [_flight(1)], DEP)
    assert d.kind == "awaiting_match" and d.reason == "no_registration"


def test_departure_inside_window_matches_and_outside_is_ignored():
    inside = _flight(1, departuretime="2026-09-24 10:20", arrivaltime="")
    outside = _flight(2, departuretime="2026-09-24 11:00", arrivaltime="")
    d = match_session(_session(landing_ts=T + timedelta(hours=1)), [outside, inside], BUNDLE)
    assert d.kind == "matched" and d.flid == 1 and d.reason == "departure_time_match"


def test_timed_candidate_beats_untimed_ones():
    timed = _flight(5, departuretime="2026-09-24 09:50")
    untimed_a = _flight(3)
    untimed_b = _flight(4)
    d = match_session(_session(landing_ts=T + timedelta(hours=1)),
                      [untimed_a, timed, untimed_b], BUNDLE)
    assert d.kind == "matched" and d.flid == 5


def test_two_open_flights_same_callsign_are_ambiguous():
    d = match_session(_session(), [_flight(2), _flight(1)], DEP)
    assert d.kind == "ambiguous" and d.flid is None
    assert d.reason == "ambiguous_match:1,2"


def test_two_timed_candidates_in_window_are_ambiguous():
    a = _flight(1, departuretime="2026-09-24 09:45")
    b = _flight(2, departuretime="2026-09-24 10:10")
    d = match_session(_session(landing_ts=T + timedelta(hours=1)), [a, b], BUNDLE)
    assert d.kind == "ambiguous"


def test_starttype_conflict_drops_candidate_and_flags():
    winch = _flight(1, starttype=5)         # VF says winch, we detected aerotow
    d = match_session(_session(), [winch], DEP)
    assert d.kind == "starttype_conflict" and d.conflict_flids == [1]
    assert d.reason == "starttype_conflict:1"

    other = _flight(2)                      # empty starttype, still open
    d = match_session(_session(), [winch, other], DEP)
    assert d.kind == "matched" and d.flid == 2 and d.conflict_flids == [1]


def test_compatible_starttype_is_fine():
    d = match_session(_session(), [_flight(1, starttype=3)], DEP)   # 3 = aerotow
    assert d.kind == "matched"


def test_flight_with_all_target_fields_set_is_not_a_candidate():
    done = _flight(1, departuretime="2026-09-24 10:01")
    d = match_session(_session(), [done], DEP)
    assert d.kind == "awaiting_match"


def test_landingcount_target_requires_higher_session_count():
    f = _flight(1, departuretime="2026-09-24 10:01", arrivaltime="2026-09-24 12:00", landingcount=1)
    d = match_session(_session(landing_count=1), [f], {"landingcount"})
    assert d.kind == "awaiting_match"
    d = match_session(_session(landing_count=2), [f], {"landingcount"})
    assert d.kind == "matched"


def test_previously_matched_flight_is_sticky():
    s = _session(matched_flid=9)
    d = match_session(s, [_flight(1), _flight(9, departuretime="2026-09-24 10:01")], BUNDLE)
    assert d.kind == "matched" and d.flid == 9 and d.reason == "previously_matched"


@pytest.mark.parametrize("detected", ["unknown", "aerotow_ambiguous", None])
def test_undetected_start_type_only_matches_empty_vf_starttype(detected):
    d = match_session(_session(start_type_detected=detected), [_flight(1, starttype=3)], DEP)
    assert d.kind == "starttype_conflict"
    d = match_session(_session(start_type_detected=detected), [_flight(1)], DEP)
    assert d.kind == "matched"
