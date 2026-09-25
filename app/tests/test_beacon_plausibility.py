"""Ground-contact plausibility in the state machine (home and foreign airports).

Ground contact needs a real altitude (> 0) within +-ground_max_agl_m of the
runway. Relay beacons with altitude 0 / speed 0 (ADS-B airliners far away)
used to satisfy ``agl_af < ground_max_agl_m`` because their AGL was hugely
negative - the next real beacon at 11 000 m then "took off at Ohlstadt".
"""

from app.tracking.airports import Airport, AirportIndex
from app.tracking.flight_state import FlightStatus
from app.tracking.flight_state_machine import _ground_plausible
from tests.conftest import AF_ELEV, Sim, beacon, ground_roll, make_config, pos

GLD = "GLD001"
JET = "3E71D4"


def _cfg_ground():
    return make_config()


# ---------------------------------------------------------------------------
# _ground_plausible
# ---------------------------------------------------------------------------

def test_ground_band_is_symmetric_around_the_reference():
    cfg = make_config(ground_max_agl_m=50)
    assert _ground_plausible(AF_ELEV, AF_ELEV, cfg)
    assert _ground_plausible(AF_ELEV + 30, AF_ELEV, cfg)
    assert _ground_plausible(AF_ELEV - 30, AF_ELEV, cfg)
    assert not _ground_plausible(AF_ELEV + 60, AF_ELEV, cfg)
    assert not _ground_plausible(AF_ELEV - 60, AF_ELEV, cfg)


def test_zero_altitude_is_never_ground_contact_above_sea_level():
    cfg = make_config(elevation_m=10.0, ground_max_agl_m=50)
    # within the band relative to a 10 m field, but no altitude data
    assert not _ground_plausible(0.0, 10.0, cfg)
    assert not _ground_plausible(-5.0, 10.0, cfg)
    assert _ground_plausible(5.0, 10.0, cfg)


def test_zero_altitude_is_ground_contact_at_a_sea_level_reference():
    """Home field at sea level: 0 m is a real altitude, GPS noise goes negative."""
    cfg = make_config(elevation_m=0.0, ground_max_agl_m=50)
    assert _ground_plausible(0.0, 0.0, cfg)
    assert _ground_plausible(-10.0, 0.0, cfg)
    assert not _ground_plausible(-60.0, 0.0, cfg)


def test_foreign_airport_at_sea_level_uses_its_own_elevation_not_home():
    """The reference is the airport the aircraft is at, not the home field.

    Home at 600 m, foreign airport at 0 m, beacon at 0 m: plausible ground
    contact at the foreign airport - the old implementation looked at
    ``config.elevation_m`` (600 m > 0) and rejected the 0 m beacon.
    """
    cfg = make_config(elevation_m=600.0, ground_max_agl_m=50)
    assert _ground_plausible(0.0, 0.0, cfg)
    assert _ground_plausible(-10.0, 0.0, cfg)
    # ... and at home the same 0 m beacon is still no ground contact
    assert not _ground_plausible(0.0, cfg.elevation_m, cfg)


# ---------------------------------------------------------------------------
# Home airfield
# ---------------------------------------------------------------------------

def test_relay_beacon_then_airliner_never_takes_off(sim: Sim):
    sim.feed_all([
        beacon(JET, 0, alt=0, speed=0),
        beacon(JET, 5, alt=0, speed=0),
        beacon(JET, 10, east=500, alt=11000, speed=800, device_type=9),
        beacon(JET, 15, east=1500, alt=11000, speed=800, device_type=9),
        beacon(JET, 20, east=2500, alt=11000, speed=800, device_type=9),
    ])
    assert sim.flight(JET) is None
    assert JET not in sim.sm._ground_cache.get("test", {})
    assert sim.event_types(JET) == []


def test_real_glider_on_the_field_takes_off(sim: Sim):
    flight = sim.feed_all(ground_roll(GLD))
    assert flight is not None and flight.status == FlightStatus.TAKEOFF


def test_beacon_slightly_below_field_elevation_counts_as_ground(sim: Sim):
    """GPS noise: a parked glider reported 30 m under the runway is parked."""
    roll = ground_roll(GLD)
    low = [beacon(GLD, 0, speed=0, alt=AF_ELEV - 30), beacon(GLD, 3, speed=0, alt=AF_ELEV - 30)]
    flight = sim.feed_all(low + roll[2:])
    assert GLD in sim.sm._ground_cache.get("test", {}) or flight is not None
    assert flight is not None and flight.status == FlightStatus.TAKEOFF
    assert sim.event_types(GLD)[:1] == ["takeoff"]


def test_beacon_far_below_field_elevation_is_not_ground(sim: Sim):
    """Below the band = no ground contact (mirror of 'well above')."""
    sim.feed_all([beacon(GLD, 0, speed=0, alt=AF_ELEV - 200),
                  beacon(GLD, 3, speed=0, alt=AF_ELEV - 200)])
    assert GLD not in sim.sm._ground_cache.get("test", {})
    # ... and therefore no takeoff on the following fast + high beacons
    flight = sim.feed_all(ground_roll(GLD)[3:])
    assert flight is None and sim.flight(GLD) is None


# ---------------------------------------------------------------------------
# Foreign airport (visitor departure bookkeeping)
# ---------------------------------------------------------------------------

def _airport_index(lat: float, lon: float, elevation_m: float | None) -> AirportIndex:
    idx = AirportIndex()
    idx.add(Airport(ident="EDXX", name="Far Field", latitude=lat, longitude=lon,
                    elevation_m=elevation_m, icao_code="EDXX"))
    return idx


def test_relay_beacon_is_no_ground_contact_at_foreign_airport():
    cfg = make_config()
    sim = Sim(cfg)
    lat, lon = pos(east_m=20_000)
    sim.sm.airports = _airport_index(lat, lon, 500.0)
    b = beacon(JET, 0, east=20_000, alt=0, speed=0)
    sim.feed(b)
    assert JET not in sim.sm._foreign_ground.get("test", {})


def test_glider_slightly_below_foreign_airport_is_ground_contact():
    cfg = make_config()
    sim = Sim(cfg)
    lat, lon = pos(east_m=20_000)
    sim.sm.airports = _airport_index(lat, lon, 500.0)
    sim.feed(beacon(GLD, 0, east=20_000, alt=480, speed=0))
    assert GLD in sim.sm._foreign_ground.get("test", {})


def test_zero_altitude_beacon_at_sea_level_foreign_airport_is_ground_contact():
    """Home at 600 m, foreign airport at 0 m, beacon 0 m / slow -> parked there."""
    cfg = make_config(elevation_m=600.0)
    sim = Sim(cfg)
    lat, lon = pos(east_m=20_000)
    sim.sm.airports = _airport_index(lat, lon, 0.0)
    sim.feed(beacon(GLD, 0, east=20_000, alt=0, speed=5))
    assert GLD in sim.sm._foreign_ground.get("test", {})
