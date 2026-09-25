"""Foreign airfields and visitors (state machine + tracker replay tests).

Covers:
- landing at a known foreign airport = normal landing (landing_type
  'foreign', landing_airfield set), no outlanding
- no airport nearby -> OUTLANDING as before (landing_type 'outlanding')
- foreign landing needs the home altitude band and is retracted when the
  aircraft is clearly airborne again away from the airport
- return after a confirmed outlanding: airborne again -> old flight
  archived (flight_restarted), new flight from the field; re-appearing on
  the ground at home -> old flight archived (outlanding_returned), the
  aircraft is forgotten (no flight without a takeoff time)
- visitors: ground contact + takeoff at a foreign airport, entering the
  visitor zone -> FlightState with takeoff airfield / time; never seen on
  the ground -> takeoff unknown; landing at home -> landing_type 'home';
  leaving the zone / falling silent / going down in the field / landing
  at another airport -> dropped without alarms (never OUTLANDING_PENDING,
  OUTLANDING, SIGNAL_LOST, ALARM); visitor_zone_km = 0 disables visitors
- restart from a foreign landing starts at that airport
- FlightState round trip of the new fields
"""

import time
from unittest.mock import AsyncMock

from app.tracking.airports import Airport, AirportIndex
from app.tracking.flight_state import FlightState, FlightStatus
from app.tracking.flight_state_machine import (
    UNKNOWN_AIRFIELD_NAME,
    UNKNOWN_FIELD_NAME,
    _iso_from_ts,
)
from app.tracking.flight_tracker import FlightTracker
from tests.conftest import (
    AF_ELEV,
    T0,
    Sim,
    approach_and_land,
    beacon,
    fly_away,
    ground_roll,
    make_config,
    pos,
)
from tests.test_flight_tracker import FakeRedisWriter, FakeResolver

GLD = "GLD001"
VIS = "VIS001"

# A known airport 20 km east of home, 60 m lower
AP_EAST = 20_000
AP_ELEV = 600.0
AP_NAME = "Testfeld"


def make_index(*airports: Airport) -> AirportIndex:
    idx = AirportIndex()
    if not airports:
        lat, lon = pos(AP_EAST, 0)
        airports = (Airport(ident="EDXX", name=AP_NAME, latitude=lat, longitude=lon,
                            elevation_m=AP_ELEV, icao_code="EDXX"),)
    for a in airports:
        idx.add(a)
    return idx


def sim_with_airports(**cfg) -> Sim:
    sim = Sim(make_config(name="Heimat", **cfg))
    sim.sm.airports = make_index()
    return sim


def sm_events(sim: Sim, fid: str = GLD) -> list[str]:
    return [t for t in sim.event_types(fid) if t != "launch_type_detected"]


def _airborne_flight(sim: Sim, fid: str = GLD):
    sim.feed_all(ground_roll(fid))
    sim.feed_all(fly_away(fid, 30))
    return sim.flight(fid)


def approach_foreign(fid: str, t0: float, east: float = AP_EAST, alt0: float = AP_ELEV):
    """Final approach and rollout at the foreign airport (touchdown t0+12)."""
    return [
        beacon(fid, t0 + 0, east=east + 300, alt=alt0 + 40, speed=90, vs=-2.0),
        beacon(fid, t0 + 3, east=east + 200, alt=alt0 + 20, speed=80, vs=-1.5),
        beacon(fid, t0 + 6, east=east + 100, alt=alt0 + 5, speed=60, vs=-1.0),
        beacon(fid, t0 + 9, east=east + 50, alt=alt0 + 1, speed=45, vs=0.0),
        beacon(fid, t0 + 12, east=east + 20, alt=alt0, speed=30, vs=0.0),
        beacon(fid, t0 + 15, east=east + 10, alt=alt0, speed=15, vs=0.0),
        beacon(fid, t0 + 18, east=east + 5, alt=alt0, speed=5, vs=0.0),
        beacon(fid, t0 + 22, east=east, alt=alt0, speed=0, vs=0.0),
    ]


def foreign_ground_roll(fid: str, t0: float, east: float = AP_EAST, alt0: float = AP_ELEV):
    """Parked, then taking off at the foreign airport (roll starts t0+9)."""
    return [
        beacon(fid, t0 + 0, east=east, alt=alt0, speed=0),
        beacon(fid, t0 + 3, east=east, alt=alt0, speed=0),
        beacon(fid, t0 + 6, east=east + 5, alt=alt0, speed=5),
        beacon(fid, t0 + 9, east=east + 30, alt=alt0, speed=45),
        beacon(fid, t0 + 12, east=east + 80, alt=alt0 + 15, speed=80, vs=2.0),
        beacon(fid, t0 + 15, east=east + 160, alt=alt0 + 60, speed=95, vs=3.0),
    ]


def inbound(fid: str, t0: float, east: float = 10_000, n: int = 2, alt: float = AF_ELEV + 500):
    """Airborne beacons inside the visitor zone (east of home)."""
    return [beacon(fid, t0 + i * 3, east=east - i * 80, alt=alt, speed=95) for i in range(n)]


def confirm_outlanding(sim: Sim, fid: str = GLD):
    """Age the pending suspicion past outlanding_timeout_s and run the timer."""
    flight = sim.flight(fid)
    assert flight.status == FlightStatus.OUTLANDING_PENDING
    flight.outlanding_pending_since = time.monotonic() - 400
    sim.timeouts()
    assert sim.flight(fid).status == FlightStatus.OUTLANDING


# ---------------------------------------------------------------------------
# Home landings carry the new fields
# ---------------------------------------------------------------------------

def test_home_flight_has_takeoff_and_landing_airfield():
    sim = sim_with_airports()
    flight = _airborne_flight(sim)
    assert flight.takeoff_airfield == "Heimat"
    assert flight.landing_type == "" and flight.landing_airfield == ""
    assert flight.is_visitor is False
    flight = sim.feed_all(approach_and_land(GLD, 600))
    assert flight.status == FlightStatus.LANDING
    assert flight.landing_type == "home"
    assert flight.landing_airfield == "Heimat"


def test_airfield_name_falls_back_to_slug(sim: Sim):
    flight = _airborne_flight(sim)
    assert flight.takeoff_airfield == "test"


# ---------------------------------------------------------------------------
# P2: landing at a known foreign airport
# ---------------------------------------------------------------------------

def test_landing_at_known_airport_is_a_foreign_landing_not_an_outlanding():
    sim = sim_with_airports()
    _airborne_flight(sim)
    flight = sim.feed_all(approach_foreign(GLD, 600))

    assert flight.status == FlightStatus.LANDING
    assert flight.landing_type == "foreign"
    assert flight.landing_airfield == "Testfeld (EDXX)"
    assert flight.landing_method == "observed"
    assert flight.landing_time == _iso_from_ts(T0 + 612)
    assert "outlanding" not in sm_events(sim)
    assert FlightStatus.OUTLANDING_PENDING not in [
        e["flight"].status for e in sim.events]
    landing = sim.events_of("landing", GLD)[0]
    assert landing["message"] == "Landung in Testfeld (EDXX)"


def test_foreign_landing_becomes_final_and_survives_timeouts():
    sim = sim_with_airports()
    _airborne_flight(sim)
    sim.feed_all(approach_foreign(GLD, 600))
    flight = sim.feed(beacon(GLD, 710, east=AP_EAST, alt=AP_ELEV, speed=0))
    assert flight.landing_final is True
    assert flight.status == FlightStatus.LANDING
    sim.timeouts()
    assert sim.flight(GLD).status == FlightStatus.LANDING
    assert "signal_lost" not in sm_events(sim)


def test_no_airport_nearby_is_an_outlanding_as_before(sim: Sim):
    sim.sm.airports = AirportIndex()  # empty index = legacy behaviour
    _airborne_flight(sim)
    flight = sim.feed_all(approach_foreign(GLD, 600))
    assert flight.status == FlightStatus.OUTLANDING_PENDING
    assert flight.landing_type == ""
    confirm_outlanding(sim)
    flight = sim.flight(GLD)
    assert flight.landing_type == "outlanding"
    assert flight.landing_airfield == ""
    # Landing time = start of the suspicion (first slow + low beacon)
    assert flight.landing_time == _iso_from_ts(T0 + 612)
    assert sm_events(sim)[-1] == "outlanding"


def test_airport_out_of_radius_is_an_outlanding():
    sim = sim_with_airports()
    _airborne_flight(sim)
    # Lands 3 km short of the airport (foreign_airfield_radius_m = 2000)
    flight = sim.feed_all(approach_foreign(GLD, 600, east=AP_EAST - 3000))
    assert flight.status == FlightStatus.OUTLANDING_PENDING


def test_slow_pass_high_above_airport_is_not_a_landing_there():
    """Slow and 100 m over terrain but 900 m above the airport elevation
    (slope 2 km from a valley airport): outlanding suspicion, not a landing."""
    sim = sim_with_airports()
    _airborne_flight(sim)
    alt = AP_ELEV + 900
    for i in range(6):
        flight = sim.sm.process_beacon(
            beacon(GLD, 600 + i * 3, east=AP_EAST + 500, alt=alt, speed=30),
            sim.config, terrain_m=alt - 100,
        )
    assert flight.status == FlightStatus.OUTLANDING_PENDING


def test_foreign_landing_needs_the_same_altitude_band_as_home():
    """Slow low pass 1.5 km from a valley airport, 140 m above its elevation
    (100 m over the slope): outside the +-near_ground_band_m -> no landing."""
    sim = sim_with_airports()
    _airborne_flight(sim)
    alt = AP_ELEV + 140
    for i in range(6):
        flight = sim.sm.process_beacon(
            beacon(GLD, 600 + i * 3, east=AP_EAST + 1500, alt=alt, speed=30),
            sim.config, terrain_m=alt - 100,
        )
    assert flight.status == FlightStatus.OUTLANDING_PENDING
    assert flight.landing_type == "" and flight.landing_airfield == ""
    assert "landing" not in sm_events(sim)
    # Within the band it is a landing (real foreign landing still works)
    sim2 = sim_with_airports()
    _airborne_flight(sim2)
    flight = sim2.feed_all(approach_foreign(GLD, 600, alt0=AP_ELEV + 50))
    assert flight.status == FlightStatus.LANDING and flight.landing_type == "foreign"


def test_foreign_landing_is_retracted_when_aircraft_climbs_away():
    sim = sim_with_airports()
    _airborne_flight(sim)
    flight = sim.feed_all(approach_foreign(GLD, 600))
    assert flight.status == FlightStatus.LANDING and flight.landing_type == "foreign"
    assert flight.landing_final is False

    # 3 km from the airport, 340 m above the airfield, fast: never landed
    flight = sim.feed(beacon(GLD, 640, east=AP_EAST + 3000, alt=AF_ELEV + 340, speed=95, vs=1.0))
    assert flight.status == FlightStatus.FLYING
    assert flight.landing_type == "" and flight.landing_airfield == ""
    assert flight.landing_time == "" and flight.landing_final is False
    assert flight.landing_count == 1
    assert sm_events(sim)[-1] == "landing_retracted"
    assert "flight_restarted" not in sm_events(sim) and "touch_and_go" not in sm_events(sim)
    # Same flight lands at home afterwards
    flight = sim.feed_all(approach_and_land(GLD, 3000))
    assert flight.status == FlightStatus.LANDING and flight.landing_type == "home"
    assert flight.takeoff_time == _iso_from_ts(T0 + 9)


def test_final_foreign_landing_is_not_retracted():
    sim = sim_with_airports()
    _airborne_flight(sim)
    sim.feed_all(approach_foreign(GLD, 600))
    flight = sim.feed(beacon(GLD, 710, east=AP_EAST, alt=AP_ELEV, speed=0))
    assert flight.landing_final is True
    flight = sim.feed(beacon(GLD, 720, east=AP_EAST + 3000, alt=AF_ELEV + 340, speed=95))
    assert flight.status == FlightStatus.LANDING
    assert "landing_retracted" not in sm_events(sim)


# ---------------------------------------------------------------------------
# P1: return after a confirmed outlanding
# ---------------------------------------------------------------------------

def test_outlanding_recovered_when_airborne_again_starts_new_flight(sim: Sim):
    _airborne_flight(sim)
    first_takeoff = sim.flight(GLD).takeoff_time
    sim.feed_all(approach_foreign(GLD, 600))
    confirm_outlanding(sim)

    # One airborne beacon is not enough (glitch guard) ...
    flight = sim.feed(beacon(GLD, 2000, east=AP_EAST + 100, alt=AF_ELEV + 400, speed=95))
    assert flight.status == FlightStatus.OUTLANDING
    assert flight.takeoff_time == first_takeoff
    # ... the second one is
    flight = sim.feed(beacon(GLD, 2003, east=AP_EAST + 200, alt=AF_ELEV + 400, speed=95))
    assert flight.status == FlightStatus.FLYING
    assert flight.takeoff_time == _iso_from_ts(T0 + 2000)
    assert flight.takeoff_airfield == UNKNOWN_FIELD_NAME
    assert flight.landing_type == "" and flight.landing_time == ""
    assert flight.is_visitor is False

    restarted = sim.events_of("flight_restarted", GLD)
    assert len(restarted) == 1
    old = restarted[0]["flight"]
    assert old.takeoff_time == first_takeoff
    assert old.landing_type == "outlanding"
    assert old.landing_time == _iso_from_ts(T0 + 612)
    assert sm_events(sim)[-2:] == ["flight_restarted", "takeoff"]

    # ... and the new flight lands at home normally
    flight = sim.feed_all(approach_and_land(GLD, 3000))
    assert flight.status == FlightStatus.LANDING
    assert flight.landing_type == "home"
    assert flight.landing_time == _iso_from_ts(T0 + 3012)


def test_outlanding_recovered_from_known_airport_names_it():
    sim = sim_with_airports()
    sim.sm.airports = AirportIndex()   # outlanding first (no airport known) ...
    _airborne_flight(sim)
    sim.feed_all(approach_foreign(GLD, 600))
    confirm_outlanding(sim)
    sim.sm.airports = make_index()     # ... index loaded later (config reload)
    sim.feed(beacon(GLD, 2000, east=AP_EAST + 100, alt=AF_ELEV + 400, speed=95))
    flight = sim.feed(beacon(GLD, 2003, east=AP_EAST + 200, alt=AF_ELEV + 400, speed=95))
    assert flight.status == FlightStatus.FLYING
    assert flight.takeoff_airfield == "Testfeld (EDXX)"


def test_outlanded_aircraft_reappearing_on_the_ground_at_home_is_forgotten(sim: Sim):
    """D-KFGT case: outlanded, came back (trailer / radio hole), parked at home.

    The outlanding flight is archived; NO flight without a takeoff time is
    invented - the aircraft is a plain home ground contact again.
    """
    _airborne_flight(sim)
    first_takeoff = sim.flight(GLD).takeoff_time
    sim.feed_all(approach_foreign(GLD, 600))
    confirm_outlanding(sim)

    assert sim.feed(beacon(GLD, 5000, east=10, alt=AF_ELEV, speed=8)) is None
    assert sim.flight(GLD) is None                         # not tracked any more
    assert sm_events(sim)[-1] == "outlanding_returned"
    assert "takeoff" not in sm_events(sim)[-2:] and "flight_restarted" not in sm_events(sim)
    old = sim.events_of("outlanding_returned", GLD)[0]["flight"]
    assert old.takeoff_time == first_takeoff
    assert old.landing_type == "outlanding"
    assert old.landing_time == _iso_from_ts(T0 + 612)
    assert old.landing_airfield == ""
    # Ground contact at home started with that very beacon ...
    assert GLD in sim.sm._ground_cache["test"]
    # ... so a later real takeoff is a normal home flight with a takeoff time
    sim.feed(beacon(GLD, 5100, east=5, alt=AF_ELEV, speed=0))
    flight = sim.feed_all(ground_roll(GLD, 6000))
    assert flight is not None and flight.status == FlightStatus.TAKEOFF
    assert flight.takeoff_time == _iso_from_ts(T0 + 6009)
    assert flight.takeoff_airfield == "test"
    assert flight.is_visitor is False and flight.landing_type == ""
    assert sm_events(sim)[-1] == "takeoff"


def test_no_flight_ever_starts_without_a_takeoff_time(sim: Sim):
    """Every FlightState the machine emits (except visitors) has a takeoff time."""
    _airborne_flight(sim)
    sim.feed_all(approach_foreign(GLD, 600))
    confirm_outlanding(sim)
    sim.feed(beacon(GLD, 2000, east=AP_EAST + 100, alt=AF_ELEV + 400, speed=95))
    sim.feed(beacon(GLD, 2003, east=AP_EAST + 200, alt=AF_ELEV + 400, speed=95))
    sim.feed_all(approach_and_land(GLD, 3000))
    sim.feed(beacon(GLD, 5000, east=10, alt=AF_ELEV, speed=8))
    for e in sim.events:
        f = e["flight"]
        if not f.is_visitor:
            assert f.takeoff_time, e["event_type"]
            assert f.takeoff_time != f.landing_time


def test_outlanded_aircraft_still_in_the_field_keeps_status(sim: Sim):
    _airborne_flight(sim)
    sim.feed_all(approach_foreign(GLD, 600))
    confirm_outlanding(sim)
    flight = sim.feed(beacon(GLD, 2000, east=AP_EAST + 5, alt=AP_ELEV, speed=2))
    assert flight.status == FlightStatus.OUTLANDING
    assert "flight_restarted" not in sm_events(sim)


# ---------------------------------------------------------------------------
# P3: visitors
# ---------------------------------------------------------------------------

def test_visitor_with_known_departure_gets_takeoff_airfield_and_time():
    sim = sim_with_airports()
    for b in foreign_ground_roll(VIS, 0):
        assert sim.feed(b) is None            # no FlightState yet
    assert sim.flight(VIS) is None
    deps = sim.sm._foreign_departures["test"]
    assert VIS in deps and deps[VIS]["takeoff_ts"] == T0 + 9
    assert sim.sm._foreign_ground["test"] == {}

    # 20 km away: outside the zone, still nothing
    assert sim.feed(beacon(VIS, 100, east=19_000, alt=AF_ELEV + 500, speed=95)) is None
    # Inside the zone: first beacon = candidate, second = visitor
    assert sim.feed(inbound(VIS, 200)[0]) is None
    flight = sim.feed(inbound(VIS, 200)[1])
    assert flight is not None
    assert flight.status == FlightStatus.FLYING
    assert flight.is_visitor is True
    assert flight.takeoff_airfield == "Testfeld (EDXX)"
    assert flight.takeoff_time == _iso_from_ts(T0 + 9)
    assert flight.visitor_since == _iso_from_ts(T0 + 200)
    assert flight.launch_type == "unknown"
    assert sim.event_types(VIS) == ["visitor_arrived"]
    assert sim.events_of("visitor_arrived", VIS)[0]["message"] == "Besucher aus Testfeld (EDXX)"
    assert VIS not in deps                    # consumed


def test_visitor_never_seen_on_ground_has_unknown_departure():
    sim = sim_with_airports()
    flight = sim.feed_all(inbound(VIS, 200))
    assert flight.is_visitor is True
    assert flight.takeoff_airfield == UNKNOWN_AIRFIELD_NAME
    assert flight.takeoff_time == ""
    assert flight.visitor_since == _iso_from_ts(T0 + 200)


def test_visitor_lands_at_home():
    sim = sim_with_airports()
    sim.feed_all(foreign_ground_roll(VIS, 0))
    sim.feed_all(inbound(VIS, 200))
    flight = sim.feed_all(approach_and_land(VIS, 600))
    assert flight.status == FlightStatus.LANDING
    assert flight.landing_type == "home"
    assert flight.landing_airfield == "Heimat"
    assert flight.is_visitor is True
    assert flight.takeoff_airfield == "Testfeld (EDXX)"
    assert sim.event_types(VIS) == ["visitor_arrived", "landing"]
    # Restart at home after the final landing: a regular home flight
    sim.feed(beacon(VIS, 720, speed=0))
    sim.feed(beacon(VIS, 900, east=20, speed=45))
    flight = sim.feed(beacon(VIS, 903, east=150, alt=AF_ELEV + 60, speed=95, vs=3.0))
    assert flight.status == FlightStatus.TAKEOFF
    assert flight.is_visitor is False
    assert flight.takeoff_airfield == "Heimat"


def test_visitor_leaving_the_zone_is_dropped_silently():
    sim = sim_with_airports()
    sim.feed_all(inbound(VIS, 200))
    assert sim.flight(VIS) is not None
    # 17 km: inside the 1.2 x hysteresis, still tracked
    assert sim.feed(beacon(VIS, 300, east=17_000, alt=AF_ELEV + 500, speed=95)) is not None
    # 19 km: gone
    assert sim.feed(beacon(VIS, 310, east=19_000, alt=AF_ELEV + 500, speed=95)) is None
    assert sim.flight(VIS) is None
    assert sim.event_types(VIS) == ["visitor_arrived", "visitor_left"]


def test_silent_visitor_is_dropped_instead_of_alarmed():
    sim = sim_with_airports()
    flight = sim.feed_all(inbound(VIS, 200))
    flight.last_seen = _iso_from_ts(time.time() - 600)
    sim.timeouts()
    assert sim.flight(VIS) is None
    assert "signal_lost" not in sim.event_types(VIS)
    assert sim.event_types(VIS)[-1] == "visitor_left"


def test_slow_low_visitor_without_airport_is_dropped_not_alarmed():
    """A visitor going down in the field is dropped: never OUTLANDING_PENDING,
    OUTLANDING, SIGNAL_LOST or ALARM."""
    sim = sim_with_airports()
    flight = sim.feed_all(inbound(VIS, 200))
    assert flight.is_visitor
    # 8 km east (no airport within 2 km), 50 m AGL, slow
    for i in range(4):
        flight = sim.feed(beacon(VIS, 300 + i * 3, east=8000, alt=AF_ELEV + 50, speed=30))
    assert flight is None
    assert sim.flight(VIS) is None
    assert sim.event_types(VIS) == ["visitor_arrived", "visitor_left"]
    assert sim.events_of("visitor_left", VIS)[0]["message"] == "Besucher wieder weg (outlanding)"
    for e in sim.events:
        assert e["flight"].status not in (
            FlightStatus.OUTLANDING_PENDING, FlightStatus.OUTLANDING,
            FlightStatus.SIGNAL_LOST, FlightStatus.ALARM, FlightStatus.EMERGENCY,
        )
    sim.timeouts()
    assert sim.events_of("signal_lost") == [] and sim.events_of("alarm") == []


def test_visitor_in_outlanding_pending_from_old_hot_state_is_dropped_by_timer():
    sim = sim_with_airports()
    flight = sim.feed_all(inbound(VIS, 200))
    flight.status = FlightStatus.OUTLANDING_PENDING       # written by an older worker
    flight.outlanding_pending_since = time.monotonic() - 400
    sim.timeouts()
    assert sim.flight(VIS) is None
    assert sim.event_types(VIS) == ["visitor_arrived", "visitor_left"]
    assert "outlanding" not in sim.event_types(VIS)


def test_visitor_landing_at_another_airport_in_the_zone_is_dropped():
    lat, lon = pos(10_000, 0)
    near = Airport(ident="EDYY", name="Nachbar", latitude=lat, longitude=lon,
                   elevation_m=AF_ELEV, icao_code="EDYY")
    sim = sim_with_airports()
    sim.sm.airports = make_index(near)
    sim.feed_all(inbound(VIS, 200, east=13_000))
    assert sim.flight(VIS) is not None
    sim.feed_all(approach_foreign(VIS, 300, east=10_000, alt0=AF_ELEV))
    assert sim.flight(VIS) is None
    assert sim.event_types(VIS) == ["visitor_arrived", "visitor_left"]
    assert sim.events_of("visitor_left", VIS)[0]["message"] == "Besucher wieder weg (landed_elsewhere)"
    assert "landing" not in sim.event_types(VIS)


def test_visitor_zone_zero_disables_visitor_detection():
    sim = sim_with_airports(visitor_zone_km=0)
    assert sim.feed_all(inbound(VIS, 200, east=3000)) is None
    assert sim.feed_all(inbound(VIS, 300, east=500)) is None
    assert sim.flight(VIS) is None
    assert sim.sm._visitor_candidates.get("test", {}) == {}
    assert sim.events == []
    # Foreign departures are still recorded (independent feature)
    sim.feed_all(foreign_ground_roll(VIS, 400))
    assert VIS in sim.sm._foreign_departures["test"]


def test_untracked_aircraft_outside_zone_or_too_high_is_ignored():
    sim = sim_with_airports()
    # 16 km: outside the zone
    assert sim.feed_all(inbound(VIS, 0, east=16_000)) is None
    # 10 km but 1600 m above the airfield: passing traffic
    assert sim.feed_all(inbound(VIS, 100, alt=AF_ELEV + 1600)) is None
    # Slow (thermalling) - the visitor check needs takeoff speed
    assert sim.feed(beacon(VIS, 200, east=10_000, alt=AF_ELEV + 500, speed=30)) is None
    assert sim.flight(VIS) is None
    assert sim.events == []


def test_visitor_single_beacon_does_not_create_a_flight():
    sim = sim_with_airports()
    assert sim.feed(inbound(VIS, 0)[0]) is None
    # A non-qualifying beacon resets the candidate
    assert sim.feed(beacon(VIS, 3, east=10_000, alt=AF_ELEV + 500, speed=20)) is None
    assert sim.feed(inbound(VIS, 6)[0]) is None
    assert sim.flight(VIS) is None


def test_no_foreign_ground_contact_without_index(sim: Sim):
    sim.sm.airports = AirportIndex()
    sim.feed_all(foreign_ground_roll(VIS, 0))
    assert sim.sm._foreign_ground.get("test", {}) == {}
    assert sim.sm._foreign_departures.get("test", {}) == {}
    # Visitors still work (takeoff unknown)
    flight = sim.feed_all(inbound(VIS, 200))
    assert flight.is_visitor and flight.takeoff_airfield == UNKNOWN_AIRFIELD_NAME


def test_foreign_ground_contact_needs_low_altitude():
    sim = sim_with_airports()
    # Slow but 300 m above the airport: thermalling overhead, no ground contact
    sim.feed(beacon(VIS, 0, east=AP_EAST, alt=AP_ELEV + 300, speed=20))
    assert sim.sm._foreign_ground["test"] == {}


def test_foreign_dicts_are_bounded_and_expire():
    sim = sim_with_airports(foreign_ground_max_entries=3)
    for i in range(5):
        sim.feed(beacon(f"G{i}", i, east=AP_EAST, alt=AP_ELEV, speed=0))
    fg = sim.sm._foreign_ground["test"]
    assert list(fg) == ["G2", "G3", "G4"]
    # Stale entries (> 2 h without a beacon) are dropped by the timer
    fg["G2"]["last_seen"] -= 3 * 3600
    deps = sim.sm._foreign_departures.setdefault("test", {})
    deps["OLD"] = {"takeoff_ts": time.time() - 13 * 3600, "airport": None}
    deps["NEW"] = {"takeoff_ts": time.time() - 3600, "airport": None}
    sim.timeouts()
    assert list(fg) == ["G3", "G4"]
    assert list(deps) == ["NEW"]


# ---------------------------------------------------------------------------
# Restart from a foreign landing
# ---------------------------------------------------------------------------

def test_restart_from_foreign_landing_starts_at_that_airport():
    sim = sim_with_airports()
    _airborne_flight(sim)
    sim.feed_all(approach_foreign(GLD, 600))
    flight = sim.feed(beacon(GLD, 710, east=AP_EAST, alt=AP_ELEV, speed=0))
    assert flight.landing_final is True
    sim.feed(beacon(GLD, 800, east=AP_EAST + 30, alt=AP_ELEV, speed=45))
    flight = sim.feed(beacon(GLD, 803, east=AP_EAST + 160, alt=AP_ELEV + 60, speed=95, vs=3.0))
    # Away from home the new flight is FLYING on the very same beacon
    # (TAKEOFF -> FLYING when not at home)
    assert flight.status == FlightStatus.FLYING
    assert flight.takeoff_time == _iso_from_ts(T0 + 800)
    assert flight.takeoff_airfield == "Testfeld (EDXX)"
    assert flight.landing_type == "" and flight.landing_time == ""
    assert sm_events(sim)[-2:] == ["flight_restarted", "takeoff"]
    old = sim.events_of("flight_restarted", GLD)[0]["flight"]
    assert old.landing_type == "foreign"
    assert old is not flight


def test_touch_and_go_at_foreign_airport_continues_flight():
    sim = sim_with_airports()
    _airborne_flight(sim)
    sim.feed_all(approach_foreign(GLD, 600))
    # 40 s after touchdown: rolling again and lifting off
    sim.feed(beacon(GLD, 652, east=AP_EAST + 30, alt=AP_ELEV, speed=45))
    flight = sim.feed(beacon(GLD, 655, east=AP_EAST + 160, alt=AP_ELEV + 60, speed=95, vs=3.0))
    assert flight.status == FlightStatus.FLYING
    assert flight.landing_count == 2
    assert flight.landing_type == "" and flight.landing_airfield == ""
    assert "touch_and_go" in sm_events(sim)


def test_restored_foreign_landing_uses_airport_as_reference():
    sim = sim_with_airports()
    lat, lon = pos(AP_EAST, 0)
    flight = FlightState(
        flarm_id=GLD, airfield_slug="test", status=FlightStatus.LANDING,
        latitude=lat, longitude=lon, altitude_m=AP_ELEV,
        landing_time=_iso_from_ts(T0 + 100), landing_type="foreign",
        landing_airfield="Testfeld (EDXX)", landing_final=True,
    )
    sim.sm.restore_flight("test", flight)
    assert flight._landing_ref_elev == AP_ELEV
    sim.feed(beacon(GLD, 800, east=AP_EAST + 30, alt=AP_ELEV, speed=45))
    new = sim.feed(beacon(GLD, 803, east=AP_EAST + 160, alt=AP_ELEV + 60, speed=95, vs=3.0))
    assert new is not flight
    assert new.status == FlightStatus.FLYING
    assert new.takeoff_airfield == "Testfeld (EDXX)"
    assert new.takeoff_time == _iso_from_ts(T0 + 800)


# ---------------------------------------------------------------------------
# FlightState persistence
# ---------------------------------------------------------------------------

def test_redis_round_trip_keeps_foreign_fields():
    f = FlightState(
        flarm_id="X", airfield_slug="test", takeoff_airfield="Testfeld (EDXX)",
        landing_airfield="Heimat", landing_type="home", is_visitor=True,
        visitor_since="2026-09-25T10:00:00Z",
    )
    data = f.to_redis_dict()
    assert data["is_visitor"] == "1"
    assert all(isinstance(v, str) for v in data.values())
    g = FlightState.from_redis(data, "test")
    assert g.takeoff_airfield == "Testfeld (EDXX)"
    assert g.landing_airfield == "Heimat"
    assert g.landing_type == "home"
    assert g.is_visitor is True
    assert g.visitor_since == "2026-09-25T10:00:00Z"
    legacy = FlightState.from_redis({"flarm_id": "X", "status": "2"}, "test")
    assert legacy.is_visitor is False and legacy.takeoff_airfield == ""


# ---------------------------------------------------------------------------
# Tracker wiring
# ---------------------------------------------------------------------------

def _tracker(index: AirportIndex | None) -> FlightTracker:
    t = FlightTracker(FakeRedisWriter(), FakeResolver(), airports=index)
    t.set_configs({"test": make_config(name="Heimat")})
    t._archive_to_log = AsyncMock()
    t._delete_flight_status = AsyncMock()
    return t


async def _feed(tracker: FlightTracker, beacons):
    config = tracker._configs["test"]
    flight = None
    for b in beacons:
        flight = await tracker._process_beacon_for_airfield(b, config) or flight
    return flight


async def test_tracker_publishes_visitor_arrived_and_left():
    tracker = _tracker(make_index())
    assert tracker.state_machine.airports is tracker.airports
    await _feed(tracker, foreign_ground_roll(VIS, 0))
    flight = await _feed(tracker, inbound(VIS, 200))
    assert flight is not None and flight.is_visitor
    events = [e[1] for e in tracker.redis_writer.events]
    assert events == ["visitor_arrived"]
    fid, payload, msg = tracker.redis_writer.event_payloads[0]
    assert payload["is_visitor"] == "1"
    assert payload["takeoff_airfield"] == "Testfeld (EDXX)"
    assert msg == "Besucher aus Testfeld (EDXX)"
    assert ("test", VIS) in tracker.redis_writer.flights
    # No launch detection for a flight that did not start here
    assert not tracker.launch_detector.is_pending(VIS)

    await _feed(tracker, [beacon(VIS, 300, east=19_000, alt=AF_ELEV + 500, speed=95)])
    events = [e[1] for e in tracker.redis_writer.events]
    assert events == ["visitor_arrived", "visitor_left"]
    assert ("test", VIS) not in tracker.redis_writer.flights
    tracker._archive_to_log.assert_not_called()
    # Like the DDB eviction path: track stream and flight_status row go too
    assert tracker.redis_writer.deleted_tracks == [("test", VIS)]
    tracker._delete_flight_status.assert_called_once()
    assert tracker._delete_flight_status.call_args.args[0].flarm_id == VIS


async def test_tracker_archives_and_forgets_outlanding_returned_home():
    tracker = _tracker(AirportIndex())
    await _feed(tracker, ground_roll(GLD))
    await _feed(tracker, fly_away(GLD, 30))
    await _feed(tracker, approach_foreign(GLD, 600))
    flight = tracker.state_machine.get_flight("test", GLD)
    flight.outlanding_pending_since = time.monotonic() - 400
    await tracker.check_timeouts()
    assert flight.status == FlightStatus.OUTLANDING
    assert await _feed(tracker, [beacon(GLD, 5000, east=10, alt=AF_ELEV, speed=8)]) is None
    tracker._archive_to_log.assert_called_once()
    archived = tracker._archive_to_log.call_args.args[0]
    assert archived is flight and archived.landing_type == "outlanding"
    assert archived.takeoff_time and archived.takeoff_time != archived.landing_time
    assert tracker.state_machine.get_flight("test", GLD) is None
    assert ("test", GLD) not in tracker.redis_writer.flights
    events = [e[1] for e in tracker.redis_writer.events]
    assert events[-1] == "outlanding_returned"
    assert "takeoff" not in events[events.index("outlanding"):]


async def test_tracker_archives_outlanding_on_recovery():
    tracker = _tracker(AirportIndex())
    await _feed(tracker, ground_roll(GLD))
    await _feed(tracker, fly_away(GLD, 30))
    await _feed(tracker, approach_foreign(GLD, 600))
    flight = tracker.state_machine.get_flight("test", GLD)
    flight.outlanding_pending_since = time.monotonic() - 400
    await tracker.check_timeouts()
    assert flight.status == FlightStatus.OUTLANDING
    await _feed(tracker, [
        beacon(GLD, 2000, east=AP_EAST + 100, alt=AF_ELEV + 400, speed=95),
        beacon(GLD, 2003, east=AP_EAST + 200, alt=AF_ELEV + 400, speed=95),
    ])
    tracker._archive_to_log.assert_called_once()
    archived = tracker._archive_to_log.call_args.args[0]
    assert archived.landing_type == "outlanding"
    assert archived.takeoff_time == flight.takeoff_time
    new = tracker.state_machine.get_flight("test", GLD)
    assert new is not flight and new.status == FlightStatus.FLYING
    assert new.takeoff_airfield == UNKNOWN_FIELD_NAME
    events = [e[1] for e in tracker.redis_writer.events]
    assert events[-2:] == ["flight_restarted", "takeoff"]


async def test_process_line_offers_tracked_aircraft_only_to_its_airfield():
    """An aircraft flying for airfield B must not become a visitor of A."""
    tracker = FlightTracker(FakeRedisWriter(), FakeResolver(), airports=make_index())
    cfg_a = make_config(slug="a", name="A", latitude=47.64 + 0.09)   # 10 km north
    cfg_b = make_config(slug="b", name="B")
    tracker.set_configs({"a": cfg_a, "b": cfg_b})
    tracker._archive_to_log = AsyncMock()
    # Takeoff at B (home of B, 10 km from A = inside A's visitor zone)
    for b in ground_roll(GLD):
        await tracker._process_beacon_for_airfield(b, cfg_b)
    assert tracker.state_machine.get_flight("b", GLD) is not None
    # Now via process_line: airborne 3 km east of B, 10 km from A
    for b in fly_away(GLD, 30):
        await tracker._process_beacon_for_airfield(b, cfg_b)
    # Simulate the line path with the same beacon for both configs
    from app.tracking import flight_tracker as ft
    b = fly_away(GLD, 60)[0]
    orig_parse = ft.parse_beacon
    ft.parse_beacon = lambda line: b
    try:
        await tracker.process_line("raw")
    finally:
        ft.parse_beacon = orig_parse
    assert tracker.state_machine.get_flight("a", GLD) is None
    assert tracker.state_machine.get_flight("b", GLD).status == FlightStatus.FLYING
