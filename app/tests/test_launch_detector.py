"""Replay tests for launch type detection.

Aerotow pairing (scoring, one-to-one, ambiguity), AGL release altitude,
tow duration, tow-plane-side fallback, winch (configurable threshold),
self-launch and a-priori powered classification.
"""

from app.tracking.flight_state_machine import _iso_from_ts
from tests.conftest import (
    AF_ELEV,
    T0,
    Sim,
    approach_and_land,
    beacon,
    ground_roll,
    make_config,
)

GLD = "GLD001"
GLD2 = "GLD002"
TOW = "TOW001"
TOW2 = "TOW002"

TOW_REGS = {TOW: "D-ETOW", TOW2: "D-ETWO", GLD: "D-1234", GLD2: "D-5678"}


def _interleave(*sequences):
    """Round-robin merge of equally long beacon lists (tow first)."""
    return [b for group in zip(*sequences) for b in group]


def pair_climb(fid: str, t_start: float, n: int, *, north: float = 0.0,
               east_offset: float = 0.0, alt_offset: float = 0.0,
               speed: float = 100, device_type: int = 0) -> list[beacon]:
    """Straight climb-out from the lift-off point, one beacon every 3 s.

    Position i: east = 150 + 80*(i+1), alt = AF_ELEV + 60 + 7.5*(i+1).
    """
    out = []
    for i in range(n):
        t = t_start + 3 * i
        out.append(beacon(
            fid, t,
            east=150 + 80 * (i + 1) + east_offset,
            north=north,
            alt=AF_ELEV + 60 + 7.5 * (i + 1) + alt_offset,
            speed=speed, vs=2.5, track=90,
            device_type=device_type,
        ))
    return out


def _sim(roles=None, models=None, config=None) -> Sim:
    return Sim(config=config, roles=roles, models=models, registrations=TOW_REGS)


# ---------------------------------------------------------------------------
# A priori roles
# ---------------------------------------------------------------------------

def test_tow_plane_is_powered_a_priori():
    sim = _sim(roles={TOW: "towplane"})
    flight = sim.feed_all(ground_roll(TOW))

    assert flight.launch_type == "powered"
    assert flight.pairing_confidence == 1.0
    events = sim.events_of("launch_type_detected", TOW)
    assert len(events) == 1


def test_flarm_aircraft_type_is_role_fallback():
    sim = _sim()  # no configured roles at all
    flight = sim.feed_all(ground_roll(TOW, device_type=2))
    assert flight.flarm_aircraft_type == 2
    assert flight.launch_type == "powered"


def test_airfield_tow_list_is_role_fallback():
    sim = _sim(config=make_config(tow_plane_flarm_ids=[TOW]))
    flight = sim.feed_all(ground_roll(TOW))
    assert flight.launch_type == "powered"


# ---------------------------------------------------------------------------
# Aerotow
# ---------------------------------------------------------------------------

def _tow_and_glider_to_release(sim: Sim, n_climb: int = 50):
    """Tow + glider take off together, climb attached, then separate."""
    sim.feed_all(_interleave(ground_roll(TOW), ground_roll(GLD)))
    tow_seq = pair_climb(TOW, 18, n_climb, east_offset=50)   # 50 m ahead
    gld_seq = pair_climb(GLD, 18, n_climb)
    sim.feed_all(_interleave(tow_seq, gld_seq))
    last_t = 18 + 3 * (n_climb - 1)
    last_alt = AF_ELEV + 60 + 7.5 * n_climb
    last_east = 150 + 80 * n_climb

    # Separation: tow plane turns away and descends, glider continues
    sim.feed(beacon(TOW, last_t + 3, east=last_east + 450, north=200,
                    alt=last_alt - 40, speed=140, vs=-3.0, track=45))
    flight = sim.feed(beacon(GLD, last_t + 3, east=last_east + 60,
                             alt=last_alt + 2, speed=85, vs=0.5, track=90))
    return flight, last_t, last_alt


def test_aerotow_release_altitude_agl_time_and_duration():
    sim = _sim(roles={TOW: "towplane", GLD: "glider"})
    glider, last_t, last_alt = _tow_and_glider_to_release(sim)

    assert glider.launch_type == "aerotow"
    assert glider.tow_plane_flarm_id == TOW
    assert glider.tow_plane_reg == "D-ETOW"
    assert glider.release_alt_m == last_alt
    assert glider.release_alt_agl_m == last_alt - AF_ELEV
    assert glider.release_time == _iso_from_ts(T0 + last_t)
    assert glider.tow_duration_s == last_t - 9   # roll started at t=9
    assert glider.release_method == "pair_separation"
    assert glider.pairing_confidence >= 0.9

    events = sim.events_of("launch_type_detected", GLD)
    assert len(events) == 1
    assert events[0]["flight"].launch_type == "aerotow"

    # The tow plane itself is never classified as a towed glider
    assert sim.flight(TOW).launch_type == "powered"
    assert sim.flight(TOW).tow_plane_flarm_id == ""


def test_aerotow_pairs_without_any_role_information():
    """No fleet roles, no FLARM type: pairing still works by geometry and
    the partner that sinks after the separation is the tow plane."""
    sim = _sim()
    glider, last_t, last_alt = _tow_and_glider_to_release(sim)
    assert glider.launch_type == "aerotow"
    assert glider.tow_plane_flarm_id == TOW
    assert glider.release_alt_agl_m == last_alt - AF_ELEV

    tow = sim.flight(TOW)
    assert tow.launch_type == "powered"
    assert tow.tow_plane_flarm_id == ""
    assert tow.release_alt_agl_m == 0


def _roleless_pair_to_separation(sim: Sim, n_climb: int = 50):
    sim.feed_all(_interleave(ground_roll(TOW), ground_roll(GLD)))
    sim.feed_all(_interleave(pair_climb(TOW, 18, n_climb, east_offset=50),
                             pair_climb(GLD, 18, n_climb)))
    last_t = 18 + 3 * (n_climb - 1)
    last_alt = AF_ELEV + 60 + 7.5 * n_climb
    last_east = 150 + 80 * n_climb
    return last_t, last_alt, last_east


def test_roleless_pair_glider_beacon_first_while_tug_still_climbing():
    """Reviewer scenario: the glider's separation beacon arrives first and
    the glider sinks a little (-1.2) while the tug's stored VS is still
    +2.5. Nobody may be classified from that; the next beacons (tug pushes
    over to -3) decide - glider gets the height, tug is powered."""
    sim = _sim()
    last_t, last_alt, last_east = _roleless_pair_to_separation(sim)
    t = last_t + 3
    # Tug already 450 m away but its beacon still shows climb
    sim.feed(beacon(TOW, t, east=last_east + 450, north=200, alt=last_alt + 5,
                    speed=140, vs=2.5, track=45))
    glider = sim.feed(beacon(GLD, t, east=last_east + 60, alt=last_alt - 3,
                             speed=95, vs=-1.2, track=90))
    assert glider.launch_type == "unknown"           # undecided, waiting
    assert sim.flight(TOW).launch_type == "unknown"

    t += 3
    sim.feed(beacon(TOW, t, east=last_east + 800, north=400, alt=last_alt - 10,
                    speed=150, vs=-3.0, track=45))
    glider = sim.feed(beacon(GLD, t, east=last_east + 120, alt=last_alt - 6,
                             speed=95, vs=-1.0, track=90))

    assert glider.launch_type == "aerotow"
    assert glider.tow_plane_flarm_id == TOW
    assert glider.release_alt_agl_m == last_alt - AF_ELEV
    tow = sim.flight(TOW)
    assert tow.launch_type == "powered"
    assert tow.release_alt_agl_m == 0
    assert tow.tow_plane_flarm_id == ""


def test_roleless_pair_undecidable_gets_no_height_for_either():
    """Both partners sink moderately after separation: abstain."""
    sim = _sim()
    last_t, last_alt, last_east = _roleless_pair_to_separation(sim)
    for i in range(1, 14):  # 39 s of gentle sink on both sides
        t = last_t + 3 * i
        sim.feed(beacon(TOW, t, east=last_east + 300 + 100 * i, north=200,
                        alt=last_alt - 5 * i, speed=130, vs=-1.6, track=45))
        sim.feed(beacon(GLD, t, east=last_east + 60 + 60 * i,
                        alt=last_alt - 4 * i, speed=95, vs=-1.3, track=90))

    for fid in (TOW, GLD):
        f = sim.flight(fid)
        assert f.launch_type == "aerotow_ambiguous", fid
        assert f.release_alt_m == 0
        assert f.release_alt_agl_m == 0
        assert f.release_time == ""


def test_same_tow_plane_two_consecutive_tows_first_glider_silent():
    """Stale assignment must not block the next tow of the same tug."""
    sim = _sim(roles={TOW: "towplane", GLD: "glider", GLD2: "glider"})
    sim.feed_all(_interleave(ground_roll(TOW), ground_roll(GLD)))
    sim.feed_all(_interleave(pair_climb(TOW, 18, 15, east_offset=50),
                             pair_climb(GLD, 18, 15)))
    # Glider goes silent; tug climbs on (no descent seen: receiver gap),
    # then reappears on final and lands.
    sim.feed_all(pair_climb(TOW, 63, 30, east_offset=50))      # t=63..150
    top_alt = sim.flight(TOW).altitude_m
    sim.feed_all(approach_and_land(TOW, 400))
    assert sim.flight(GLD).launch_type == "unknown"             # still pending
    sim.feed(beacon(TOW, 520, speed=0))                         # landing final

    # Second tow with GLD2, 20 minutes later
    sim.feed_all(_interleave(ground_roll(TOW, 1200), ground_roll(GLD2, 1200)))
    # The tug's restart closes the first detection with what was known
    glider1 = sim.flight(GLD)
    assert glider1.launch_type == "aerotow"
    assert glider1.release_method == "towplane_max"
    assert glider1.release_alt_m == top_alt

    n = 50
    sim.feed_all(_interleave(pair_climb(TOW, 1218, n, east_offset=50),
                             pair_climb(GLD2, 1218, n)))
    last_t = 1218 + 3 * (n - 1)
    last_alt = AF_ELEV + 60 + 7.5 * n
    last_east = 150 + 80 * n
    sim.feed(beacon(TOW, last_t + 3, east=last_east + 450, north=300, alt=last_alt - 40, speed=140, vs=-3))
    glider2 = sim.feed(beacon(GLD2, last_t + 3, east=last_east + 60, alt=last_alt, speed=85, vs=0.5))

    assert glider2.launch_type == "aerotow"
    assert glider2.tow_plane_flarm_id == TOW
    assert glider2.release_alt_agl_m == last_alt - AF_ELEV
    assert glider2.tow_duration_s == last_t - 1209
    # First glider's altitude was not polluted by the second climb
    assert sim.flight(GLD).release_alt_m == top_alt


def test_long_tow_keeps_tracking_beyond_three_minutes():
    """A 6-minute tow must still yield a release altitude."""
    sim = _sim(roles={TOW: "towplane"})
    glider, last_t, last_alt = _tow_and_glider_to_release(sim, n_climb=115)
    assert last_t > 300
    assert glider.launch_type == "aerotow"
    assert glider.release_alt_agl_m == last_alt - AF_ELEV
    assert glider.tow_duration_s == last_t - 9


def test_short_proximity_is_not_a_tow():
    sim = _sim(roles={TOW: "towplane"})
    sim.feed_all(_interleave(ground_roll(TOW), ground_roll(GLD)))
    # Together for only ~45 s, then apart
    sim.feed_all(_interleave(pair_climb(TOW, 18, 15, east_offset=50),
                             pair_climb(GLD, 18, 15)))
    sim.feed(beacon(TOW, 63, east=2000, north=500, alt=AF_ELEV + 200, speed=140, vs=-2))
    sim.feed(beacon(GLD, 63, east=1400, alt=AF_ELEV + 175, speed=90, vs=0))
    glider = sim.flight(GLD)
    assert glider.launch_type == "unknown"
    assert glider.tow_plane_flarm_id == ""

    # Window ends without any other signature
    glider = sim.feed(beacon(GLD, 200, east=3000, alt=AF_ELEV + 300, speed=90))
    assert glider.launch_type == "unknown"
    assert glider.release_alt_agl_m == 0


def test_parallel_tows_with_equal_scores_are_ambiguous():
    sim = _sim(roles={TOW: "towplane", TOW2: "towplane"})
    sim.feed_all(_interleave(ground_roll(TOW), ground_roll(TOW2), ground_roll(GLD)))
    n = 50
    sim.feed_all(_interleave(
        pair_climb(TOW, 18, n, north=20, east_offset=50),
        pair_climb(TOW2, 18, n, north=-20, east_offset=50),
        pair_climb(GLD, 18, n),
    ))
    last_t = 18 + 3 * (n - 1)
    last_alt = AF_ELEV + 60 + 7.5 * n
    last_east = 150 + 80 * n
    sim.feed(beacon(TOW, last_t + 3, east=last_east + 450, north=300, alt=last_alt - 40, speed=140, vs=-3))
    sim.feed(beacon(TOW2, last_t + 3, east=last_east + 450, north=-300, alt=last_alt - 40, speed=140, vs=-3))
    glider = sim.feed(beacon(GLD, last_t + 3, east=last_east + 60, alt=last_alt, speed=85, vs=0.5))

    assert glider.launch_type == "aerotow_ambiguous"
    assert glider.release_alt_m == 0
    assert glider.release_alt_agl_m == 0
    assert glider.release_time == ""
    assert glider.pairing_confidence <= 0.5


def test_one_tow_plane_is_assigned_to_one_glider_only():
    sim = _sim(roles={TOW: "towplane", GLD: "glider", GLD2: "glider"})
    sim.feed_all(_interleave(ground_roll(TOW), ground_roll(GLD), ground_roll(GLD2)))
    n = 62  # > 180 s so GLD2's window closes
    sim.feed_all(_interleave(
        pair_climb(TOW, 18, n, east_offset=50),     # tow 50 m ahead of GLD
        pair_climb(GLD, 18, n),
        pair_climb(GLD2, 18, n, east_offset=-80),   # 130 m behind the tow
    ))
    last_t = 18 + 3 * (n - 1)
    last_alt = AF_ELEV + 60 + 7.5 * n
    last_east = 150 + 80 * n
    sim.feed(beacon(TOW, last_t + 3, east=last_east + 450, north=300, alt=last_alt - 40, speed=140, vs=-3))
    glider = sim.feed(beacon(GLD, last_t + 3, east=last_east + 60, alt=last_alt, speed=85, vs=0.5))
    glider2 = sim.feed(beacon(GLD2, last_t + 3, east=last_east - 20, alt=last_alt, speed=85, vs=0.5))

    assert glider.launch_type == "aerotow"
    assert glider.tow_plane_flarm_id == TOW
    assert glider.release_alt_agl_m == last_alt - AF_ELEV
    # The second glider never got the tow plane and ends up unclassified
    assert glider2.launch_type == "unknown"
    assert glider2.tow_plane_flarm_id == ""
    assert glider2.release_alt_agl_m == 0


def test_towplane_max_fallback_when_glider_track_is_gappy():
    sim = _sim(roles={TOW: "towplane"})
    sim.feed_all(_interleave(ground_roll(TOW), ground_roll(GLD)))
    # Attached for 15 beacons, then the glider goes silent
    sim.feed_all(_interleave(pair_climb(TOW, 18, 15, east_offset=50),
                             pair_climb(GLD, 18, 15)))
    assert sim.flight(GLD).launch_type == "unknown"  # still pending

    # Tow plane climbs on alone until t=200 ...
    tow_alone = pair_climb(TOW, 63, 46, east_offset=50)  # t=63..198
    sim.feed_all(tow_alone)
    top = sim.feed(beacon(TOW, 200, east=5000, alt=AF_ELEV + 460, speed=100, vs=2.0))
    top_alt = top.altitude_m
    # ... then descends
    for i, t in enumerate((203, 206, 209)):
        glider = sim.flight(GLD)
        sim.feed(beacon(TOW, t, east=5100 + 100 * i, alt=top_alt - 10 * (i + 1),
                        speed=140, vs=-2.5, track=270))

    glider = sim.flight(GLD)
    assert glider.launch_type == "aerotow"
    assert glider.release_method == "towplane_max"
    assert glider.release_alt_m == top_alt
    assert glider.release_alt_agl_m == top_alt - AF_ELEV
    assert glider.release_time == _iso_from_ts(T0 + 200)
    assert glider.tow_duration_s == 200 - 9
    assert 0.7 <= glider.pairing_confidence <= 0.9


# ---------------------------------------------------------------------------
# Winch / self-launch / unknown
# ---------------------------------------------------------------------------

def _winch_launch(sim: Sim):
    sim.feed_all(ground_roll(GLD))
    alt = AF_ELEV + 60
    for i, t in enumerate(range(18, 48, 3)):
        alt += 36
        sim.feed(beacon(GLD, t, east=150 + 30 * (i + 1), alt=alt, speed=95, vs=12.0))
    # Release: VS collapses
    return sim.feed(beacon(GLD, 48, east=500, alt=alt + 10, speed=80, vs=1.0)), alt + 10


def test_winch_release_altitude_and_time():
    sim = _sim()
    flight, release_alt = _winch_launch(sim)

    assert flight.launch_type == "winch"
    assert flight.release_alt_m == release_alt
    assert flight.release_alt_agl_m == release_alt - AF_ELEV
    assert flight.release_time == _iso_from_ts(T0 + 48)
    assert flight.release_method == "winch_vs_drop"
    assert flight.tow_duration_s == 0
    assert len(sim.events_of("launch_type_detected", GLD)) == 1


def test_winch_threshold_comes_from_airfield_config():
    sim = _sim(config=make_config(winch_vs_threshold_ms=15.0))
    flight, _ = _winch_launch(sim)
    assert flight.launch_type == "unknown"  # 12 m/s is below the field's threshold

    flight = sim.feed(beacon(GLD, 200, east=3000, alt=AF_ELEV + 300, speed=90))
    assert flight.launch_type == "unknown"
    assert flight.release_alt_agl_m == 0


def test_self_launch_from_model_after_quiet_minute():
    sim = _sim(models={GLD: "Arcus M"})
    sim.feed_all(ground_roll(GLD))
    flight = sim.flight(GLD)
    assert flight.launch_type == "unknown"  # not decided at takeoff

    for i, t in enumerate(range(18, 66, 3)):
        flight = sim.feed(beacon(GLD, t, east=150 + 80 * (i + 1),
                                 alt=AF_ELEV + 60 + 9 * (i + 1), speed=110, vs=3.0))
    assert flight.launch_type == "unknown"  # < 60 s

    flight = sim.feed(beacon(GLD, 72, east=1700, alt=AF_ELEV + 240, speed=110, vs=3.0))
    assert flight.launch_type == "self"
    assert flight.release_alt_agl_m == 0


def test_motorglider_role_can_still_be_winch_launched():
    sim = _sim(roles={GLD: "motorglider_sl"})
    flight, release_alt = _winch_launch(sim)
    assert flight.launch_type == "winch"
    assert flight.release_alt_agl_m == release_alt - AF_ELEV
