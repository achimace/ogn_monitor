"""Replay tests for the flight state machine.

Covers takeoff/landing timing on beacon time, touch & go vs. final
landing vs. bounce, silence landings and the signal-loss escalation.
"""

import time

from app.tracking.flight_state import FlightStatus
from app.tracking.flight_state_machine import _iso_from_ts
from tests.conftest import (
    AF_ELEV,
    T0,
    Sim,
    approach_and_land,
    beacon,
    fly_away,
    ground_roll,
    make_config,
)

GLD = "GLD001"


def sm_events(sim: Sim, fid: str = GLD) -> list[str]:
    """State-machine event types only (launch detection is tested elsewhere)."""
    return [t for t in sim.event_types(fid) if t != "launch_type_detected"]


def _airborne_flight(sim: Sim, fid: str = GLD):
    sim.feed_all(ground_roll(fid))
    sim.feed_all(fly_away(fid, 30))
    return sim.flight(fid)


# ---------------------------------------------------------------------------
# Takeoff
# ---------------------------------------------------------------------------

def test_takeoff_time_is_start_of_ground_roll(sim: Sim):
    flight = sim.feed_all(ground_roll(GLD))

    assert flight is not None
    assert flight.status == FlightStatus.TAKEOFF
    # Roll started at t=9 (first beacon >= takeoff speed), lift-off seen at t=15
    assert flight.takeoff_time == _iso_from_ts(T0 + 9)
    assert sm_events(sim) == ["takeoff"]


def _glitch(fid: str, t: float):
    """A single GPS glitch beacon: speed and altitude jump at once."""
    return beacon(fid, t, east=40, alt=AF_ELEV + 120, speed=150, vs=0.0)


def test_single_glitch_beacon_on_ground_does_not_start_a_flight(sim: Sim):
    sim.feed(beacon(GLD, 0, speed=0))
    sim.feed(beacon(GLD, 3, speed=0))
    # One glitch: (0 + 0 + 150) / 3 = 50 km/h >= 40 and 120 m high
    assert sim.feed(_glitch(GLD, 6)) is None
    assert sim.flight(GLD) is None
    # Back to normal: still sitting on the apron, nothing was started
    assert sim.feed(beacon(GLD, 9, speed=0)) is None
    assert sim.feed(beacon(GLD, 12, speed=2)) is None
    assert sim.flight(GLD) is None
    assert sim.events == []


def test_glitch_beacon_starts_a_flight_with_legacy_setting():
    """Contrast: takeoff_min_fast_beacons=1 is the old behaviour."""
    sim = Sim(make_config(takeoff_min_fast_beacons=1))
    sim.feed(beacon(GLD, 0, speed=0))
    sim.feed(beacon(GLD, 3, speed=0))
    flight = sim.feed(_glitch(GLD, 6))
    assert flight is not None and flight.status == FlightStatus.TAKEOFF


def test_takeoff_declared_on_second_fast_beacon_keeps_first_fast_time(sim: Sim):
    """Only one fast beacon before lift-off: the declaration waits for the
    next fast beacon, the takeoff time is still the first fast beacon."""
    sim.feed(beacon(GLD, 0, speed=0))
    sim.feed(beacon(GLD, 3, speed=5))
    # First fast beacon is already 60 m high (fast winch launch, sparse beacons)
    assert sim.feed(beacon(GLD, 6, east=100, alt=AF_ELEV + 60, speed=90, vs=4.0)) is None
    assert sim.flight(GLD) is None
    flight = sim.feed(beacon(GLD, 9, east=200, alt=AF_ELEV + 120, speed=95, vs=5.0))

    assert flight is not None and flight.status == FlightStatus.TAKEOFF
    assert flight.takeoff_time == _iso_from_ts(T0 + 6)
    assert sm_events(sim) == ["takeoff"]


def test_glitch_after_aborted_roll_does_not_start_a_flight(sim: Sim):
    """A slow beacon resets the confirmation counter."""
    sim.feed(beacon(GLD, 0, speed=0))
    sim.feed(beacon(GLD, 3, east=10, speed=45))   # fast, on the ground
    sim.feed(beacon(GLD, 6, east=15, speed=10))   # roll aborted
    assert sim.feed(_glitch(GLD, 9)) is None
    assert sim.flight(GLD) is None


def test_single_glitch_beacon_on_landed_aircraft_is_not_a_touch_and_go(sim: Sim):
    _airborne_flight(sim)
    sim.feed_all(approach_and_land(GLD, 600))
    assert sim.flight(GLD).status == FlightStatus.LANDING

    flight = sim.feed(_glitch(GLD, 640))
    assert flight.status == FlightStatus.LANDING
    assert flight.landing_count == 1
    assert flight.landing_time == _iso_from_ts(T0 + 612)

    flight = sim.feed(beacon(GLD, 643, speed=0))
    assert flight.status == FlightStatus.LANDING
    assert "touch_and_go" not in sm_events(sim)
    assert "landing_retracted" not in sm_events(sim)


def test_single_glitch_beacon_on_final_landed_aircraft_is_not_a_restart(sim: Sim):
    _airborne_flight(sim)
    sim.feed_all(approach_and_land(GLD, 600))
    flight = sim.feed(beacon(GLD, 720, speed=0))
    assert flight.landing_final is True
    first_takeoff = flight.takeoff_time

    flight = sim.feed(_glitch(GLD, 800))
    assert flight.status == FlightStatus.LANDING
    assert flight.takeoff_time == first_takeoff
    assert "flight_restarted" not in sm_events(sim)
    assert sm_events(sim).count("takeoff") == 1
    # Parked aircraft keeps beaconing: the glitch's roll reference is dropped
    flight = sim.feed(beacon(GLD, 803, speed=0))
    assert flight.status == FlightStatus.LANDING

    # A real restart afterwards still works (two fast beacons)
    sim.feed(beacon(GLD, 900, east=20, speed=45))
    flight = sim.feed(beacon(GLD, 903, east=150, alt=AF_ELEV + 60, speed=95, vs=3.0))
    assert flight.status == FlightStatus.TAKEOFF
    assert flight.takeoff_time == _iso_from_ts(T0 + 900)
    assert sm_events(sim)[-2:] == ["flight_restarted", "takeoff"]


def test_overflight_without_ground_contact_is_ignored(sim: Sim):
    assert sim.feed(beacon(GLD, 0, alt=AF_ELEV + 300, speed=100)) is None
    assert sim.feed(beacon(GLD, 3, east=200, alt=AF_ELEV + 300, speed=100)) is None
    assert sim.flight(GLD) is None
    assert sim.events == []


def test_takeoff_to_flying_when_leaving_home(sim: Sim):
    flight = _airborne_flight(sim)
    assert flight.status == FlightStatus.FLYING
    assert flight.landing_count == 1


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------

def test_landing_time_is_touchdown_not_end_of_hysteresis(sim: Sim):
    _airborne_flight(sim)
    flight = sim.feed_all(approach_and_land(GLD, 600))

    assert flight.status == FlightStatus.LANDING
    # Slow phase started at t=612, detection happened at t=622
    assert flight.landing_time == _iso_from_ts(T0 + 612)
    assert flight.landing_method == "observed"
    assert flight.landing_confidence == 1.0
    assert flight.landing_final is False
    assert flight.landing_count == 1
    assert sm_events(sim) == ["takeoff", "landing"]


def test_landing_final_after_touch_go_window_on_ground_beacon(sim: Sim):
    _airborne_flight(sim)
    sim.feed_all(approach_and_land(GLD, 600))

    # Still on the apron 60 s after touchdown: not final yet
    flight = sim.feed(beacon(GLD, 672, speed=0))
    assert flight.landing_final is False
    assert "landing_final" not in sm_events(sim)

    # 95 s after touchdown: the touch & go window (90 s) has passed
    flight = sim.feed(beacon(GLD, 707, speed=0))
    assert flight.landing_final is True
    assert flight.status == FlightStatus.LANDING
    assert sm_events(sim) == ["takeoff", "landing", "landing_final"]


def test_landing_final_by_timeout_when_flarm_switched_off(sim: Sim):
    _airborne_flight(sim)
    sim.feed_all(approach_and_land(GLD, 600))

    # No further beacons at all (FLARM off): landing_time is ~1 h ago and
    # the last beacon is well outside the touch & go window.
    sim.flight(GLD).last_seen = _iso_from_ts(time.time() - 200)
    changed = sim.timeouts()

    flight = sim.flight(GLD)
    assert flight in changed
    assert flight.landing_final is True
    assert flight.status == FlightStatus.LANDING  # still sticky-visible
    assert sm_events(sim) == ["takeoff", "landing", "landing_final"]

    # A second tick must not emit landing_final again
    sim.timeouts()
    assert sm_events(sim).count("landing_final") == 1


def test_landing_final_not_by_timeout_while_beacons_keep_coming(sim: Sim):
    """A delayed feed must not finalize (and thus turn a T&G into a restart)."""
    _airborne_flight(sim)
    sim.feed_all(approach_and_land(GLD, 600))
    # landing_time is old in wallclock terms, but the FLARM is still heard
    assert sim.flight(GLD).last_seen  # set by the last beacon = now
    sim.timeouts()
    flight = sim.flight(GLD)
    assert flight.landing_final is False
    assert "landing_final" not in sm_events(sim)

    # The (late) re-takeoff beacons still count as touch & go
    sim.feed(beacon(GLD, 640, east=30, alt=AF_ELEV + 2, speed=40))
    sim.feed(beacon(GLD, 650, east=120, alt=AF_ELEV + 20, speed=70, vs=2.0))
    flight = sim.feed(beacon(GLD, 655, east=250, alt=AF_ELEV + 60, speed=90, vs=3.0))
    assert flight.landing_count == 2
    assert "flight_restarted" not in sm_events(sim)


# ---------------------------------------------------------------------------
# Touch & go / bounce / restart
# ---------------------------------------------------------------------------

def test_touch_and_go_continues_same_flight(sim: Sim):
    _airborne_flight(sim)
    sim.feed_all(approach_and_land(GLD, 600))
    takeoff_time = sim.flight(GLD).takeoff_time

    # Roll, accelerate, lift off again 43 s after touchdown
    sim.feed(beacon(GLD, 640, east=30, alt=AF_ELEV + 2, speed=40))
    sim.feed(beacon(GLD, 650, east=120, alt=AF_ELEV + 20, speed=70, vs=2.0))
    flight = sim.feed(beacon(GLD, 655, east=250, alt=AF_ELEV + 60, speed=90, vs=3.0))

    assert flight.status == FlightStatus.FLYING
    assert flight.landing_count == 2
    assert flight.landing_time == ""
    assert flight.landing_method == ""
    assert flight.takeoff_time == takeoff_time  # same flight
    assert flight.touch_go_confidence == 1.0    # low, slow, long enough on ground
    assert sm_events(sim) == ["takeoff", "landing", "touch_and_go"]
    assert "flight_restarted" not in sm_events(sim)

    # Second (final) landing keeps the counter and gets a fresh landing time
    sim.feed_all(fly_away(GLD, 700))
    flight = sim.feed_all(approach_and_land(GLD, 900))
    assert flight.status == FlightStatus.LANDING
    assert flight.landing_count == 2
    assert flight.landing_time == _iso_from_ts(T0 + 912)

    flight = sim.feed(beacon(GLD, 1010, speed=0))
    assert flight.landing_final is True
    final = sim.events_of("landing_final", GLD)
    assert len(final) == 1
    assert final[0]["flight"].landing_count == 2


def test_touch_and_go_confidence_reduced_for_high_fast_ground_phase(sim: Sim):
    _airborne_flight(sim)
    sim.feed_all(approach_and_land(GLD, 600))
    flight = sim.flight(GLD)
    # Pretend the "ground" phase never looked convincingly like ground
    flight._ground_min_agl = 30
    flight._ground_min_speed = 45

    sim.feed(beacon(GLD, 640, east=30, alt=AF_ELEV + 30, speed=45))
    sim.feed(beacon(GLD, 650, east=120, alt=AF_ELEV + 40, speed=70, vs=2.0))
    flight = sim.feed(beacon(GLD, 655, east=250, alt=AF_ELEV + 60, speed=90, vs=3.0))

    assert flight.landing_count == 2
    assert flight.touch_go_confidence == 0.6  # base + ground time only


def test_bounce_within_debounce_window_is_retracted(sim: Sim):
    _airborne_flight(sim)
    sim.feed_all(approach_and_land(GLD, 600))

    sim.feed(beacon(GLD, 624, east=60, alt=AF_ELEV + 20, speed=60, vs=2.0))
    flight = sim.feed(beacon(GLD, 626, east=150, alt=AF_ELEV + 65, speed=95, vs=3.0))

    # 14 s after touchdown: not a landing at all
    assert flight.status == FlightStatus.FLYING
    assert flight.landing_count == 1
    assert flight.landing_time == ""
    assert sm_events(sim) == ["takeoff", "landing", "landing_retracted"]

    # The real landing later counts as the first one
    sim.feed_all(fly_away(GLD, 700))
    flight = sim.feed_all(approach_and_land(GLD, 900))
    assert flight.status == FlightStatus.LANDING
    assert flight.landing_count == 1


def test_restart_after_final_landing_creates_new_flight(sim: Sim):
    _airborne_flight(sim)
    sim.feed_all(approach_and_land(GLD, 600))
    flight = sim.feed(beacon(GLD, 720, speed=0))
    assert flight.landing_final is True
    first_takeoff = flight.takeoff_time

    sim.feed(beacon(GLD, 800, east=20, speed=45))
    sim.feed(beacon(GLD, 803, east=70, alt=AF_ELEV + 15, speed=80, vs=2.0))
    flight = sim.feed(beacon(GLD, 806, east=150, alt=AF_ELEV + 60, speed=95, vs=3.0))

    assert flight.status == FlightStatus.TAKEOFF
    assert flight.takeoff_time != first_takeoff
    # Like the first flight: start of the ground roll, not lift-off
    assert flight.takeoff_time == _iso_from_ts(T0 + 800)
    assert flight.landing_count == 1
    assert sm_events(sim)[-2:] == ["flight_restarted", "takeoff"]


def test_out_of_order_beacon_is_ignored(sim: Sim):
    flight = _airborne_flight(sim)  # last beacon at t=36
    stale = beacon(GLD, 36 - 120, east=3000, alt=AF_ELEV + 400, speed=95)
    assert sim.feed(stale) is None
    assert flight.status == FlightStatus.FLYING
    # Slightly late beacons (within tolerance) are still processed
    assert sim.feed(beacon(GLD, 36 - 30, east=3100, alt=AF_ELEV + 400, speed=95)) is flight


def test_receiver_clock_ahead_does_not_blind_the_flight(sim: Sim):
    """One beacon via a receiver whose clock runs 5 min ahead must not make
    every correct beacon afterwards look out of order."""
    flight = _airborne_flight(sim)  # last beacon at t=36
    assert sim.feed(beacon(GLD, 36 + 300, east=3200, alt=AF_ELEV + 400, speed=95)) is flight

    # Correct beacons: at most a couple are dropped, then the timeline resets
    results = [
        sim.feed(beacon(GLD, 40 + 3 * i, east=3300 + 80 * i, alt=AF_ELEV + 400, speed=95))
        for i in range(6)
    ]
    assert results[:2] == [None, None]
    assert all(r is flight for r in results[2:])
    assert flight.status == FlightStatus.FLYING


# ---------------------------------------------------------------------------
# Silence landing
# ---------------------------------------------------------------------------

def _final_approach_then_silence(sim: Sim, silent_for_s: float = 200):
    _airborne_flight(sim)
    sim.feed(beacon(GLD, 600, east=300, alt=AF_ELEV + 40, speed=85, vs=-2.0))
    flight = sim.feed(beacon(GLD, 603, east=200, alt=AF_ELEV + 20, speed=75, vs=-1.5))
    # FLARM goes quiet on the runway
    flight.last_seen = _iso_from_ts(time.time() - silent_for_s)
    return flight


def test_silence_landing_after_final_approach(sim: Sim):
    _final_approach_then_silence(sim, silent_for_s=300)
    sim.timeouts()

    flight = sim.flight(GLD)
    assert flight.status == FlightStatus.LANDING
    assert flight.landing_method == "silence"
    assert flight.landing_time == _iso_from_ts(T0 + 603)
    assert flight.landing_confidence == 0.8
    assert flight.landing_final is False
    assert sm_events(sim) == ["takeoff", "landing"]
    assert "signal_lost" not in sm_events(sim)

    # Silent for 300 s > silence_landing_s + touch_go_max_ground_s: final
    sim.timeouts()
    assert sim.flight(GLD).landing_final is True
    assert sm_events(sim)[-1] == "landing_final"


def test_silence_landing_not_final_within_grace_period(sim: Sim):
    _final_approach_then_silence(sim, silent_for_s=200)
    sim.timeouts()
    assert sim.flight(GLD).landing_method == "silence"
    sim.timeouts()  # 200 s quiet < 270 s grace
    assert sim.flight(GLD).landing_final is False


def test_silence_landing_takes_precedence_over_signal_lost(sim: Sim):
    _final_approach_then_silence(sim, silent_for_s=400)  # > signal_loss_timeout_s
    sim.timeouts()
    flight = sim.flight(GLD)
    assert flight.status == FlightStatus.LANDING
    assert flight.landing_method == "silence"
    assert "signal_lost" not in sm_events(sim)


def test_silence_candidate_reset_by_go_around(sim: Sim):
    _final_approach_then_silence(sim, silent_for_s=0)
    # Go-around: climbing and fast again over the field
    flight = sim.feed(beacon(GLD, 606, east=100, alt=AF_ELEV + 150, speed=100, vs=2.5))
    flight.last_seen = _iso_from_ts(time.time() - 400)

    sim.timeouts()
    flight = sim.flight(GLD)
    assert flight.status == FlightStatus.SIGNAL_LOST
    assert flight.landing_time == ""
    assert "landing" not in sm_events(sim)


def test_silence_landing_phantom_is_retracted_on_airborne_beacon(sim: Sim):
    _final_approach_then_silence(sim, silent_for_s=200)
    sim.timeouts()
    assert sim.flight(GLD).landing_method == "silence"

    # Aircraft re-appears airborne over the field: radio hole on final
    sim.feed(beacon(GLD, 700, east=300, alt=AF_ELEV + 70, speed=95, vs=1.0))
    flight = sim.feed(beacon(GLD, 703, east=400, alt=AF_ELEV + 80, speed=95, vs=1.0))

    assert flight.status == FlightStatus.FLYING
    assert flight.landing_time == ""
    assert flight.landing_method == ""
    assert flight.landing_count == 1
    assert sm_events(sim) == ["takeoff", "landing", "landing_retracted"]


def test_silence_landing_phantom_is_retracted_away_from_home(sim: Sim):
    """Go-around into a radio hole, re-appearing 3 km out: no landing."""
    _final_approach_then_silence(sim, silent_for_s=200)
    sim.timeouts()
    assert sim.flight(GLD).landing_method == "silence"

    flight = sim.feed(beacon(GLD, 800, east=3000, alt=AF_ELEV + 700, speed=95, vs=0.5))

    assert flight.status == FlightStatus.FLYING
    assert flight.landing_time == ""
    assert flight.landing_count == 1
    assert flight.distance_m > 2500  # position fully updated again
    assert sm_events(sim) == ["takeoff", "landing", "landing_retracted"]

    # The real landing later is the first one
    flight = sim.feed_all(approach_and_land(GLD, 1200))
    assert flight.status == FlightStatus.LANDING
    assert flight.landing_count == 1
    assert flight.landing_method == "observed"


# ---------------------------------------------------------------------------
# Signal loss / elapsed
# ---------------------------------------------------------------------------

def test_signal_lost_away_from_home_uses_wallclock_since_last_seen(sim: Sim):
    flight = _airborne_flight(sim)
    flight.last_seen = _iso_from_ts(time.time() - 400)

    sim.timeouts()
    assert flight.status == FlightStatus.SIGNAL_LOST
    assert 395 <= flight.elapsed_s <= 410
    assert sm_events(sim)[-1] == "signal_lost"


def test_no_signal_lost_shortly_after_beacon(sim: Sim):
    flight = _airborne_flight(sim)  # last_seen = now
    sim.timeouts()
    assert flight.status == FlightStatus.FLYING
    assert flight.elapsed_s < 5


# ---------------------------------------------------------------------------
# Terrain AGL (terrain_m) vs airfield elevation
# ---------------------------------------------------------------------------

def _feed_terrain(sim: Sim, b, terrain_m):
    """Sim.feed equivalent that passes terrain_m to the state machine."""
    return sim.sm.process_beacon(b, sim.config, terrain_m=terrain_m)


def test_terrain_agl_is_altitude_minus_terrain(sim: Sim):
    _airborne_flight(sim)
    # Over a 1500 m ridge, 3 km out
    flight = _feed_terrain(
        sim, beacon(GLD, 700, east=3000, alt=1700.0, speed=95), terrain_m=1500.0
    )
    assert flight.altitude_agl == 200.0
    assert flight.altitude_m == 1700.0


def test_terrain_none_falls_back_to_airfield_elevation(sim: Sim):
    _airborne_flight(sim)
    flight = _feed_terrain(
        sim, beacon(GLD, 700, east=3000, alt=1700.0, speed=95), terrain_m=None
    )
    assert flight.altitude_agl == 1700.0 - AF_ELEV


def test_outlanding_uses_terrain_agl(sim: Sim):
    """Slow and 100 m above a 1200 m plateau: outlanding suspicion even though
    the aircraft is 640 m above the airfield elevation."""
    _airborne_flight(sim)
    alt = 1300.0
    for i in range(6):
        flight = _feed_terrain(
            sim, beacon(GLD, 700 + i * 3, east=4000 + i * 20, alt=alt, speed=30),
            terrain_m=1200.0,
        )
    assert flight.status == FlightStatus.OUTLANDING_PENDING

    # Without terrain the same beacons look like a high, slow thermal
    sim2 = Sim()
    _airborne_flight(sim2)
    for i in range(6):
        flight2 = sim2.feed(beacon(GLD, 700 + i * 3, east=4000 + i * 20, alt=alt, speed=30))
    assert flight2.status == FlightStatus.FLYING


def test_outlanding_recovery_uses_terrain_agl(sim: Sim):
    _airborne_flight(sim)
    for i in range(6):
        _feed_terrain(sim, beacon(GLD, 700 + i * 3, east=4000, alt=1300.0, speed=30),
                      terrain_m=1200.0)
    assert sim.flight(GLD).status == FlightStatus.OUTLANDING_PENDING
    # Climbs to 300 m over terrain (still slow): recovered
    flight = _feed_terrain(sim, beacon(GLD, 720, east=4000, alt=1500.0, speed=30),
                           terrain_m=1200.0)
    assert flight.status == FlightStatus.FLYING


def test_landing_band_and_ground_roll_stay_airfield_based(sim: Sim):
    """A DSM cell 25 m above the runway (trees/hangar) must not break the
    landing detection: the near-ground band is airfield-relative."""
    _airborne_flight(sim)
    flight = None
    for b in approach_and_land(GLD, 600):
        flight = _feed_terrain(sim, b, terrain_m=AF_ELEV + 25.0) or flight
    assert flight.status == FlightStatus.LANDING
    assert flight.landing_method == "observed"
    # Reported AGL is terrain based (below the DSM cell)
    assert flight.altitude_agl == -25.0
    # Touch & go confidence bookkeeping is runway based, not -25
    assert flight._ground_min_agl == 0.0


def test_takeoff_ground_contact_uses_airfield_elevation(sim: Sim):
    """Ground contact / lift-off at home: airfield elevation, even with a
    (noisy) terrain value 40 m above the runway."""
    flight = None
    for b in ground_roll(GLD):
        flight = _feed_terrain(sim, b, terrain_m=AF_ELEV + 40.0) or flight
    assert flight is not None
    assert flight.status == FlightStatus.TAKEOFF


def test_silence_candidate_uses_airfield_elevation(sim: Sim):
    """Final approach at 20 m over the runway counts as a silence-landing
    candidate even if the DSM under the threshold shows a 50 m tree line."""
    _airborne_flight(sim)
    flight = _feed_terrain(
        sim, beacon(GLD, 700, east=300, alt=AF_ELEV + 20, speed=70, vs=-1.0),
        terrain_m=AF_ELEV + 50.0,
    )
    assert flight._silence_candidate_ts > 0


def test_silence_phantom_retraction_uses_terrain_agl(sim: Sim):
    """Re-appearing 100 m above a 1200 m ridge is 'clearly airborne'."""
    _final_approach_then_silence(sim, silent_for_s=200)
    sim.timeouts()
    assert sim.flight(GLD).landing_method == "silence"

    flight = _feed_terrain(
        sim, beacon(GLD, 800, east=5000, alt=1300.0, speed=95, vs=0.5),
        terrain_m=1200.0,
    )
    assert flight.status == FlightStatus.FLYING
    assert flight.landing_time == ""
    assert flight.altitude_agl == 100.0
