"""FlightTracker wiring: events, archiving and detector lifecycle.

Uses an in-memory RedisWriter stand-in; PostgreSQL archiving is stubbed.
"""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.data.aircraft_resolver import AircraftInfo
from app.tracking.flight_state import FlightState, FlightStatus
from app.tracking.flight_tracker import FlightTracker
from app.tracking.redis_writer import SIMULATED_FIELD, SIMULATED_VALUE
from tests.conftest import (
    AF_ELEV,
    approach_and_land,
    beacon,
    fly_away,
    ground_roll,
    make_config,
)

GLD = "GLD001"


class FakeRedisWriter:
    def __init__(self):
        self.flights: dict[tuple[str, str], dict] = {}
        self.events: list[tuple[str, str, str]] = []
        self.track_points: list[tuple[str, str, int]] = []
        self.beacons: list[tuple[str, str]] = []
        self.positions: list[tuple[str, str]] = []
        self.deleted_tracks: list[tuple[str, str]] = []
        self.event_payloads: list[tuple[str, dict, str]] = []

    async def update_flight(self, slug, fid, data, ttl=None):
        self.flights[(slug, fid)] = data

    async def publish_beacon(self, slug, fid, data):
        self.beacons.append((slug, fid))

    async def add_position(self, slug, fid, *args):
        self.positions.append((slug, fid))

    async def add_track_point(self, slug, fid, ts_ms, *args, retention_s=0, min_interval_s=0):
        self.track_points.append((slug, fid, ts_ms))

    async def publish_event(self, slug, etype, fid, data=None, message=""):
        self.events.append((slug, etype, fid))
        self.event_payloads.append((fid, dict(data or {}), message))

    async def remove_flight(self, slug, fid):
        self.flights.pop((slug, fid), None)

    async def delete_track(self, slug, fid):
        self.deleted_tracks.append((slug, fid))

    async def get_active_flights(self, slug):
        return {fid for (s, fid) in self.flights if s == slug}

    async def get_flight(self, slug, fid):
        return self.flights.get((slug, fid))


class FakeResolver:
    """In-memory AircraftResolver stand-in: ``infos`` is the cache."""

    def __init__(self, infos: dict[str, AircraftInfo] | None = None):
        self.infos = infos or {}
        self.aprs_updates: list[tuple[str, str]] = []

    def resolve(self, flarm_id):
        return self.infos.get(flarm_id)

    def update_from_aprs(self, flarm_id, registration):
        self.aprs_updates.append((flarm_id, registration))


@pytest.fixture
def tracker():
    t = FlightTracker(FakeRedisWriter(), FakeResolver())
    t.set_configs({"test": make_config()})
    t._archive_to_log = AsyncMock()
    t._delete_flight_status = AsyncMock()
    return t


async def _feed(tracker: FlightTracker, beacons):
    config = tracker._configs["test"]
    flight = None
    for b in beacons:
        flight = await tracker._process_beacon_for_airfield(b, config) or flight
    return flight


def _event_types(tracker: FlightTracker) -> list[str]:
    return [e[1] for e in tracker.redis_writer.events]


async def test_restart_keeps_launch_detection_of_new_flight(tracker):
    await _feed(tracker, ground_roll(GLD))
    assert tracker.launch_detector.is_pending(GLD)
    await _feed(tracker, fly_away(GLD, 30))
    await _feed(tracker, approach_and_land(GLD, 600))
    await _feed(tracker, [beacon(GLD, 720, speed=0)])  # landing_final

    # Restart: the archived flight's detection must not kill the new one
    flight = await _feed(tracker, [
        beacon(GLD, 800, east=20, speed=45),
        beacon(GLD, 803, east=70, alt=AF_ELEV + 15, speed=80, vs=2.0),
        beacon(GLD, 806, east=150, alt=AF_ELEV + 60, speed=95, vs=3.0),
    ])

    assert flight.status == FlightStatus.TAKEOFF
    assert tracker.launch_detector.is_pending(GLD)
    events = _event_types(tracker)
    assert events.count("takeoff") == 2
    assert "landing_final" in events
    assert "flight_restarted" in events
    # flight_log written for the final landing and again at restart
    assert tracker._archive_to_log.await_count == 2


async def test_launch_type_event_is_published(tracker):
    await _feed(tracker, ground_roll(GLD))
    await _feed(tracker, fly_away(GLD, 30))
    # Detection window ends without a signature -> unknown, but the event
    # must reach Redis so VF-Sync can consume it.
    await _feed(tracker, [beacon(GLD, 200, east=4000, alt=AF_ELEV + 400, speed=95)])
    assert "launch_type_detected" in _event_types(tracker)
    data = tracker.redis_writer.flights[("test", GLD)]
    assert data["launch_type"] == "unknown"
    assert data["landing_count"] == "1"


async def test_recover_from_redis_skips_simulated_flights(tracker):
    """A flight written by app.vfsync.simulate (hash field simulated=1)
    must never enter the state machine on worker restart - it would be
    periodic-synced to flight_status and archived to flight_log as a real
    flight. The Redis entry stays (monitor keeps showing it until TTL)."""
    real = FlightState(flarm_id=GLD, airfield_slug="test", registration="D-REAL",
                       status=FlightStatus.FLYING, takeoff_time="2026-09-24T10:00:00Z")
    sim = FlightState(flarm_id="SIM001", airfield_slug="test", registration="D-SIM",
                      status=FlightStatus.FLYING, takeoff_time="2026-09-24T10:05:00Z")
    sim_data = sim.to_redis_dict()
    sim_data[SIMULATED_FIELD] = SIMULATED_VALUE
    tracker.redis_writer.flights[("test", GLD)] = real.to_redis_dict()
    tracker.redis_writer.flights[("test", "SIM001")] = sim_data

    restored = await tracker.recover_from_redis()

    assert restored == 1
    recovered = tracker.state_machine.get_flight("test", GLD)
    assert recovered is not None and recovered.registration == "D-REAL"
    assert recovered.airfield_id == tracker._configs["test"].id
    assert tracker.state_machine.get_flight("test", "SIM001") is None
    # hot state untouched: the monitor still sees both, TTL cleans up the sim
    assert set(tracker.redis_writer.flights) == {("test", GLD), ("test", "SIM001")}


def test_from_redis_ignores_simulated_marker():
    data = FlightState(flarm_id="SIM001", airfield_slug="test",
                       registration="D-SIM").to_redis_dict()
    data[SIMULATED_FIELD] = SIMULATED_VALUE
    flight = FlightState.from_redis(data, "test")
    assert flight.flarm_id == "SIM001" and flight.registration == "D-SIM"
    assert not hasattr(flight, SIMULATED_FIELD)


# ---------------------------------------------------------------------------
# Per-aircraft track stream (thinned)
# ---------------------------------------------------------------------------

def _track_writes(tracker: FlightTracker, fid: str | None = None):
    return [p for p in tracker.redis_writer.track_points
            if fid is None or p[1] == fid]


def _airborne(fid: str, t: float):
    return beacon(fid, t, east=300 + t * 20, alt=AF_ELEV + 120, speed=95, vs=2.0)


def _ms(b) -> int:
    return int(b.timestamp * 1000)


async def test_track_points_are_thinned_by_beacon_time(tracker, monkeypatch):
    monkeypatch.setattr(settings, "track_min_interval_s", 5)
    # ground roll: flight exists from the takeoff beacon at t=15 on
    roll = ground_roll(GLD)
    await _feed(tracker, roll)
    assert [w[2] for w in _track_writes(tracker)] == [_ms(roll[-1])]

    # 2 s after the last write -> thinned out
    b2 = _airborne(GLD, 17)
    await _feed(tracker, [b2])
    assert len(_track_writes(tracker)) == 1

    # 6 s after the last write -> second point
    b3 = _airborne(GLD, 21)
    await _feed(tracker, [b3])
    writes = _track_writes(tracker)
    assert [w[2] for w in writes] == [_ms(roll[-1]), _ms(b3)]
    assert writes[0][:2] == ("test", GLD)


async def test_track_thinning_is_independent_per_aircraft(tracker, monkeypatch):
    monkeypatch.setattr(settings, "track_min_interval_s", 5)
    other = "GLD002"
    # takeoffs at t=15 (GLD) and t=16 (other)
    await _feed(tracker, sorted(ground_roll(GLD) + ground_roll(other, t0=1),
                                key=lambda b: b.timestamp))
    assert len(_track_writes(tracker, GLD)) == 1
    assert len(_track_writes(tracker, other)) == 1

    await _feed(tracker, [_airborne(GLD, 17), _airborne(other, 18)])
    assert len(_track_writes(tracker, GLD)) == 1
    assert len(_track_writes(tracker, other)) == 1

    # only GLD passes the interval -> only GLD gets a new point
    await _feed(tracker, [_airborne(GLD, 20), _airborne(other, 20)])
    assert len(_track_writes(tracker, GLD)) == 2
    assert len(_track_writes(tracker, other)) == 1
    assert set(tracker._last_track_ts) == {f"test:{GLD}", f"test:{other}"}


async def test_track_thinning_state_is_cleared_on_archive(tracker):
    flight = await _feed(tracker, ground_roll(GLD))
    assert f"test:{GLD}" in tracker._last_track_ts

    await tracker._archive_flight("test", flight)

    assert f"test:{GLD}" not in tracker._last_track_ts
    # the next flight of the same aircraft writes right away
    await _feed(tracker, ground_roll(GLD, t0=16))
    assert len(_track_writes(tracker)) == 2


# ---------------------------------------------------------------------------
# OGN DDB privacy flags (tracked / identified)
# ---------------------------------------------------------------------------

def _info(fid: str, *, tracked=True, identified=True, registration="",
          competition_sign="", source="ogn_ddb", role="") -> AircraftInfo:
    return AircraftInfo(
        flarm_id=fid, registration=registration, aircraft_model="ASK 21",
        competition_sign=competition_sign, aircraft_type=1, source=source,
        tracked=tracked, identified=identified, role=role,
    )


def _flying(fid: str, t: float):
    return beacon(fid, t, east=3500, alt=AF_ELEV + 400, speed=95)


async def test_untracked_device_beacons_are_dropped_entirely(tracker):
    tracker.aircraft_resolver.infos[GLD] = _info(GLD, tracked=False, registration="D-SECR")

    await _feed(tracker, ground_roll(GLD))
    await _feed(tracker, fly_away(GLD, 30))

    rw = tracker.redis_writer
    assert tracker.state_machine.get_all_active_flights() == []
    assert tracker.state_machine._ground_cache.get("test", {}) == {}
    assert rw.flights == {} and rw.events == [] and rw.track_points == []
    assert rw.beacons == [] and rw.positions == []
    assert not tracker.launch_detector.is_pending(GLD)
    tracker._archive_to_log.assert_not_awaited()
    assert tracker._untracked_drops[GLD] == 9


async def test_untracked_device_is_dropped_even_when_tenant_registered(tracker):
    """build_cache keeps tracked=False on a tenant override; the tracker
    must honour it regardless of the source."""
    tracker.aircraft_resolver.infos[GLD] = _info(
        GLD, tracked=False, registration="D-CLUB", source="tenant")
    await _feed(tracker, ground_roll(GLD))
    assert tracker.state_machine.get_all_active_flights() == []
    assert tracker.redis_writer.flights == {}


async def test_process_line_drops_untracked_before_any_airfield(tracker, monkeypatch):
    tracker.set_configs({"test": make_config(), "other": make_config(id=2, slug="other")})
    tracker.aircraft_resolver.infos[GLD] = _info(GLD, tracked=False)
    b = ground_roll(GLD)[0]
    monkeypatch.setattr("app.tracking.flight_tracker.parse_beacon", lambda line: b)
    per_airfield = AsyncMock()
    monkeypatch.setattr(tracker, "_process_beacon_for_airfield", per_airfield)

    await tracker.process_line("raw aprs line")

    per_airfield.assert_not_awaited()
    assert tracker._untracked_drops[GLD] == 1  # counted once, not per airfield


async def test_untracked_drop_is_logged_once_per_hour(tracker, monkeypatch):
    tracker.aircraft_resolver.infos[GLD] = _info(GLD, tracked=False)
    fake_log = MagicMock()
    monkeypatch.setattr("app.tracking.flight_tracker.log", fake_log)

    await _feed(tracker, ground_roll(GLD)[:3])
    assert fake_log.debug.call_count == 1
    assert fake_log.debug.call_args.kwargs == {"flarm_id": GLD, "dropped": 1}

    # An hour later the next drop is logged again with the running total
    tracker._untracked_logged_at[GLD] -= 3601
    await _feed(tracker, ground_roll(GLD)[3:4])
    assert fake_log.debug.call_count == 2
    assert fake_log.debug.call_args.kwargs == {"flarm_id": GLD, "dropped": 4}
    fake_log.info.assert_not_called()


async def test_unidentified_device_is_labelled_by_flarm_id_only(tracker):
    # build_cache already blanked registration / CN for identified=N
    tracker.aircraft_resolver.infos[GLD] = _info(GLD, identified=False)
    # ... and the APRS stream must not re-identify it either
    roll = [replace(b, registration="D-1234") for b in ground_roll(GLD)]

    flight = await _feed(tracker, roll)

    assert flight is not None and flight.status == FlightStatus.TAKEOFF
    assert flight.registration == ""
    assert flight.competition_sign == ""
    assert flight.aircraft_model == "ASK 21"
    assert tracker.aircraft_resolver.aprs_updates == []
    data = tracker.redis_writer.flights[("test", GLD)]
    assert data["registration"] == "" and data["competition_sign"] == ""
    assert data["flarm_id"] == GLD


async def test_tenant_registered_unidentified_device_keeps_tenant_registration(tracker):
    tracker.aircraft_resolver.infos[GLD] = _info(
        GLD, identified=True, registration="D-CLUB", competition_sign="CL", source="tenant")
    roll = [replace(b, registration="D-OTHER") for b in ground_roll(GLD)]

    flight = await _feed(tracker, roll)

    assert flight.registration == "D-CLUB"
    assert flight.competition_sign == "CL"
    assert tracker.redis_writer.flights[("test", GLD)]["registration"] == "D-CLUB"


async def test_aprs_registration_still_used_for_device_unknown_to_ddb(tracker):
    roll = [replace(b, registration="D-APRS") for b in ground_roll(GLD)]
    flight = await _feed(tracker, roll)
    assert flight.registration == "D-APRS"
    assert tracker.aircraft_resolver.aprs_updates == [(GLD, "D-APRS")]


async def test_ddb_reload_flipping_tracked_evicts_flying_aircraft(tracker):
    tracker.aircraft_resolver.infos[GLD] = _info(GLD, registration="D-1234")
    await _feed(tracker, ground_roll(GLD))
    await _feed(tracker, fly_away(GLD, 30))
    assert tracker.state_machine.get_flight("test", GLD).status == FlightStatus.FLYING
    assert ("test", GLD) in tracker.redis_writer.flights
    events_before = list(tracker.redis_writer.events)
    tracks_before = len(tracker.redis_writer.track_points)

    # DDB reload: the owner opted out while the flight is in progress
    tracker.aircraft_resolver.infos[GLD].tracked = False
    assert await _feed(tracker, [_flying(GLD, 60)]) is None

    rw = tracker.redis_writer
    assert tracker.state_machine.get_flight("test", GLD) is None
    assert ("test", GLD) not in rw.flights
    assert rw.deleted_tracks == [("test", GLD)]
    assert len(rw.track_points) == tracks_before
    assert rw.events == events_before          # no removal / archive event
    assert not tracker.launch_detector.is_pending(GLD)
    assert f"test:{GLD}" not in tracker._last_track_ts
    tracker._archive_to_log.assert_not_awaited()
    tracker._delete_flight_status.assert_awaited_once()
    assert tracker._delete_flight_status.await_args.args[0].flarm_id == GLD

    # Stays gone on further beacons
    assert await _feed(tracker, [_flying(GLD, 63)]) is None
    assert tracker.state_machine.get_flight("test", GLD) is None
    assert rw.events == events_before
    assert tracker._untracked_drops[GLD] == 2


async def test_eviction_keeps_pending_events_of_other_aircraft(tracker):
    tracker.aircraft_resolver.infos[GLD] = _info(GLD)
    await _feed(tracker, ground_roll(GLD))
    await _feed(tracker, fly_away(GLD, 30))
    # A pending event of another aircraft must survive the eviction
    other = FlightState(flarm_id="GLD002", airfield_slug="test")
    tracker.state_machine._emit_event("test", "takeoff", "GLD002", other)
    tracker.state_machine._emit_event("test", "takeoff", GLD, tracker.state_machine.get_flight("test", GLD))

    events_before = list(tracker.redis_writer.events)

    tracker.aircraft_resolver.infos[GLD].tracked = False
    await _feed(tracker, [_flying(GLD, 60)])

    # ... and is published by the eviction itself, not delayed until the
    # next beacon of some other aircraft
    new_events = tracker.redis_writer.events[len(events_before):]
    assert new_events == [("test", "takeoff", "GLD002")]
    assert tracker.state_machine.drain_events() == []
    assert tracker.launch_detector.drain_events() == []


async def test_check_timeouts_evicts_untracked_flight_without_beacon(tracker):
    """FLARM already off when the flag flips: no beacon will ever evict."""
    tracker.aircraft_resolver.infos[GLD] = _info(GLD)
    await _feed(tracker, ground_roll(GLD))
    await _feed(tracker, fly_away(GLD, 30))
    events_before = list(tracker.redis_writer.events)

    tracker.aircraft_resolver.infos[GLD].tracked = False
    await tracker.check_timeouts()

    assert tracker.state_machine.get_flight("test", GLD) is None
    assert ("test", GLD) not in tracker.redis_writer.flights
    assert tracker.redis_writer.deleted_tracks == [("test", GLD)]
    assert tracker.redis_writer.events == events_before
    tracker._archive_to_log.assert_not_awaited()


async def test_ground_contact_is_forgotten_when_tracked_flips(tracker):
    tracker.aircraft_resolver.infos[GLD] = _info(GLD)
    await _feed(tracker, ground_roll(GLD)[:2])   # parked at home
    assert GLD in tracker.state_machine._ground_cache["test"]

    tracker.aircraft_resolver.infos[GLD].tracked = False
    await _feed(tracker, ground_roll(GLD)[2:])

    assert GLD not in tracker.state_machine._ground_cache["test"]
    assert tracker.state_machine.get_flight("test", GLD) is None


async def test_recover_from_redis_purges_untracked_flights(tracker):
    tracker.aircraft_resolver.infos["SECRET"] = _info("SECRET", tracked=False)
    real = FlightState(flarm_id=GLD, airfield_slug="test", registration="D-REAL",
                       status=FlightStatus.FLYING, takeoff_time="2026-09-25T10:00:00Z")
    secret = FlightState(flarm_id="SECRET", airfield_slug="test",
                         status=FlightStatus.FLYING, takeoff_time="2026-09-25T10:05:00Z")
    tracker.redis_writer.flights[("test", GLD)] = real.to_redis_dict()
    tracker.redis_writer.flights[("test", "SECRET")] = secret.to_redis_dict()

    restored = await tracker.recover_from_redis()

    assert restored == 1
    assert tracker.state_machine.get_flight("test", GLD) is not None
    assert tracker.state_machine.get_flight("test", "SECRET") is None
    assert set(tracker.redis_writer.flights) == {("test", GLD)}
    assert tracker.redis_writer.deleted_tracks == [("test", "SECRET")]
    tracker._archive_to_log.assert_not_awaited()


# ---------------------------------------------------------------------------
# tracked = N flips on a tow plane: the partner must lose every reference
# ---------------------------------------------------------------------------

TOW = "TOW001"


def _tow_pair_takeoff(n_climb: int):
    """Tow plane + glider roll and climb attached (tow 50 m ahead)."""
    from tests.test_launch_detector import _interleave, pair_climb
    roll = _interleave(ground_roll(TOW), ground_roll(GLD))
    climb = _interleave(pair_climb(TOW, 18, n_climb, east_offset=50),
                        pair_climb(GLD, 18, n_climb))
    last_t = 18 + 3 * (n_climb - 1)
    last_alt = AF_ELEV + 60 + 7.5 * n_climb
    last_east = 150 + 80 * n_climb
    return roll + climb, last_t, last_alt, last_east


def _payloads_since(tracker: FlightTracker, n: int):
    return tracker.redis_writer.event_payloads[n:]


def _mentions(payloads, *needles: str) -> bool:
    for _fid, data, message in payloads:
        blob = message + " " + " ".join(str(v) for v in data.values())
        if any(n in blob for n in needles):
            return True
    return False


async def test_tow_plane_eviction_mid_tow_blanks_partner_pairing(tracker):
    tracker.aircraft_resolver.infos[TOW] = _info(TOW, registration="D-ETOW", role="towplane")
    tracker.aircraft_resolver.infos[GLD] = _info(GLD, registration="D-1234", role="glider")
    seq, last_t, _alt, _east = _tow_pair_takeoff(20)
    await _feed(tracker, seq)
    ld = tracker.launch_detector
    assert ld._pending[GLD].tow_plane_id == TOW          # pair established
    assert ld._tow_assignments == {("test", TOW): GLD}
    n_payloads = len(tracker.redis_writer.event_payloads)

    # DDB reload: the tow plane owner opted out while towing
    tracker.aircraft_resolver.infos[TOW].tracked = False
    assert await _feed(tracker, [_flying(TOW, last_t + 3)]) is None

    assert tracker.state_machine.get_flight("test", TOW) is None
    glider = tracker.state_machine.get_flight("test", GLD)
    assert glider is not None and glider.status == FlightStatus.FLYING
    assert ld._pending[GLD].tow_plane_id == "" and ld._pending[GLD].tow_plane_reg == ""
    assert ld._pending[GLD].tow_max_alt == 0.0
    assert TOW in ld._pending[GLD].rejected
    assert ld._tow_assignments == {}

    # The glider's detection runs on without the partner and never
    # reports the evicted tow plane - in the event, the hash or the flight.
    from tests.test_launch_detector import pair_climb
    await _feed(tracker, pair_climb(GLD, last_t + 6, 60))
    glider = tracker.state_machine.get_flight("test", GLD)
    assert not ld.is_pending(GLD)                        # resolved
    assert glider.tow_plane_flarm_id == "" and glider.tow_plane_reg == ""
    assert glider.launch_type != "aerotow"
    data = tracker.redis_writer.flights[("test", GLD)]
    assert data["tow_plane_flarm_id"] == "" and data["tow_plane_reg"] == ""
    assert not _mentions(_payloads_since(tracker, n_payloads), TOW, "D-ETOW")
    assert ("test", "launch_type_detected", GLD) in tracker.redis_writer.events


async def test_tow_plane_eviction_after_release_blanks_resolved_partner(tracker):
    tracker.aircraft_resolver.infos[TOW] = _info(TOW, registration="D-ETOW", role="towplane")
    tracker.aircraft_resolver.infos[GLD] = _info(GLD, registration="D-1234", role="glider")
    seq, last_t, last_alt, last_east = _tow_pair_takeoff(50)
    await _feed(tracker, seq)
    # Separation: tow plane turns away and descends, glider continues
    await _feed(tracker, [
        beacon(TOW, last_t + 3, east=last_east + 450, north=200,
               alt=last_alt - 40, speed=140, vs=-3.0, track=45),
        beacon(GLD, last_t + 3, east=last_east + 60,
               alt=last_alt + 2, speed=85, vs=0.5, track=90),
    ])
    glider = tracker.state_machine.get_flight("test", GLD)
    assert glider.launch_type == "aerotow"
    assert glider.tow_plane_flarm_id == TOW and glider.tow_plane_reg == "D-ETOW"
    assert tracker.redis_writer.flights[("test", GLD)]["tow_plane_flarm_id"] == TOW
    n_payloads = len(tracker.redis_writer.event_payloads)

    tracker.aircraft_resolver.infos[TOW].tracked = False
    await _feed(tracker, [_flying(TOW, last_t + 6)])

    glider = tracker.state_machine.get_flight("test", GLD)
    assert glider.tow_plane_flarm_id == "" and glider.tow_plane_reg == ""
    assert glider.launch_type == "aerotow"               # the glider's own data stays
    assert glider.release_alt_m == last_alt
    data = tracker.redis_writer.flights[("test", GLD)]
    assert data["tow_plane_flarm_id"] == "" and data["tow_plane_reg"] == ""
    assert data["launch_type"] == "aerotow"
    assert ("test", TOW) not in tracker.redis_writer.flights
    assert not _mentions(_payloads_since(tracker, n_payloads), TOW, "D-ETOW")


# ---------------------------------------------------------------------------
# flight_status DELETE of an evicted flight: retried in check_timeouts
# ---------------------------------------------------------------------------

def _real_status_delete(tracker: FlightTracker, monkeypatch, side_effect):
    """Undo the fixture's mock: real _delete_flight_status, fake DB/sync."""
    tracker._delete_flight_status = FlightTracker._delete_flight_status.__get__(tracker)
    monkeypatch.setattr("app.db.connection.get_db", lambda: object())
    delete = AsyncMock(side_effect=side_effect)
    tracker._state_sync.delete_flight_status = delete
    return delete


async def test_failed_flight_status_delete_is_retried_in_check_timeouts(tracker, monkeypatch):
    delete = _real_status_delete(tracker, monkeypatch, [RuntimeError("db down"), None])
    tracker.aircraft_resolver.infos[GLD] = _info(GLD)
    await _feed(tracker, ground_roll(GLD))
    await _feed(tracker, fly_away(GLD, 30))

    tracker.aircraft_resolver.infos[GLD].tracked = False
    await _feed(tracker, [_flying(GLD, 60)])

    assert delete.await_count == 1
    assert tracker.state_machine.get_flight("test", GLD) is None   # eviction went on
    assert tracker._status_delete_retry == {(1, GLD)}

    await tracker.check_timeouts()

    assert delete.await_count == 2
    assert delete.await_args.args[1:] == (1, GLD)
    assert tracker._status_delete_retry == set()

    await tracker.check_timeouts()
    assert delete.await_count == 2                                  # nothing left


async def test_flight_status_delete_retry_stays_pending_while_db_is_down(tracker, monkeypatch):
    delete = _real_status_delete(tracker, monkeypatch, RuntimeError("db down"))
    tracker.aircraft_resolver.infos[GLD] = _info(GLD)
    await _feed(tracker, ground_roll(GLD))
    tracker.aircraft_resolver.infos[GLD].tracked = False
    await _feed(tracker, [_flying(GLD, 60)])
    await tracker.check_timeouts()
    assert delete.await_count == 2
    assert tracker._status_delete_retry == {(1, GLD)}


async def test_flight_status_delete_retry_set_is_bounded(tracker, monkeypatch):
    from app.tracking.flight_tracker import STATUS_DELETE_RETRY_MAX
    _real_status_delete(tracker, monkeypatch, RuntimeError("db down"))
    tracker._status_delete_retry = {(1, f"FULL{i:03d}") for i in range(STATUS_DELETE_RETRY_MAX)}
    tracker.aircraft_resolver.infos[GLD] = _info(GLD)
    await _feed(tracker, ground_roll(GLD))
    tracker.aircraft_resolver.infos[GLD].tracked = False
    await _feed(tracker, [_flying(GLD, 60)])
    assert (1, GLD) not in tracker._status_delete_retry
    assert len(tracker._status_delete_retry) == STATUS_DELETE_RETRY_MAX


# ---------------------------------------------------------------------------
# Terrain AGL: ElevationService wiring
# ---------------------------------------------------------------------------

class FakeElevation:
    """ElevationService stand-in returning a fixed value and counting calls."""

    def __init__(self, value):
        self.value = value
        self.calls: list[tuple[float, float]] = []

    async def get(self, lat, lon):
        self.calls.append((lat, lon))
        return self.value


async def test_terrain_elevation_is_passed_to_state_machine(tracker):
    tracker.elevation = FakeElevation(1500.0)
    await _feed(tracker, ground_roll(GLD))
    flight = await _feed(tracker, [beacon(GLD, 100, east=3000, alt=1700.0, speed=95)])

    assert flight.altitude_agl == 200.0
    data = tracker.redis_writer.flights[("test", GLD)]
    assert data["altitude_agl"] == "200"


async def test_terrain_lookup_only_for_tracked_flights(tracker):
    elev = FakeElevation(1500.0)
    tracker.elevation = elev

    # Ground contact + lift-off: the aircraft is not tracked yet -> no lookup
    flight = await _feed(tracker, ground_roll(GLD))
    assert flight.status == FlightStatus.TAKEOFF
    assert elev.calls == []
    # Overflight of an unknown aircraft: no lookup either
    await _feed(tracker, [beacon("OTHER1", 50, east=2000, alt=AF_ELEV + 500, speed=100)])
    assert elev.calls == []

    # Next beacon of the tracked flight resolves the terrain
    b = beacon(GLD, 100, east=3000, alt=1700.0, speed=95)
    await _feed(tracker, [b])
    assert elev.calls == [(b.lat, b.lon)]


async def test_terrain_none_falls_back_to_airfield_elevation(tracker):
    tracker.elevation = FakeElevation(None)
    await _feed(tracker, ground_roll(GLD))
    flight = await _feed(tracker, [beacon(GLD, 100, east=3000, alt=1700.0, speed=95)])
    assert flight.altitude_agl == 1700.0 - AF_ELEV


async def test_terrain_kill_switch_skips_lookup(tracker, monkeypatch):
    monkeypatch.setattr(settings, "terrain_agl_enabled", False)
    elev = FakeElevation(1500.0)
    tracker.elevation = elev
    await _feed(tracker, ground_roll(GLD))
    flight = await _feed(tracker, [beacon(GLD, 100, east=3000, alt=1700.0, speed=95)])
    assert elev.calls == []
    assert flight.altitude_agl == 1700.0 - AF_ELEV


async def test_no_elevation_service_uses_airfield_elevation(tracker):
    assert tracker.elevation is None
    await _feed(tracker, ground_roll(GLD))
    flight = await _feed(tracker, [beacon(GLD, 100, east=3000, alt=1700.0, speed=95)])
    assert flight.altitude_agl == 1700.0 - AF_ELEV


async def test_classify_flight_end_uses_terrain_at_last_position(tracker):
    """Slow at 100 m over a 1200 m plateau, then silence: OUTLANDED with the
    terrain model; against the airfield elevation (640 m "AGL") the same
    profile is unclassifiable."""
    import time as _time

    tracker.elevation = FakeElevation(1200.0)
    await _feed(tracker, ground_roll(GLD))
    flight = await _feed(tracker, [beacon(GLD, 100, east=6000, alt=1300.0, speed=70)])
    assert flight.status == FlightStatus.FLYING

    # The profile buffer prunes by wallclock, so fill it with recent points:
    # level, constant speed (no "controlled descent"), stable course.
    now = _time.time()
    for i in range(12):
        tracker.profile_buffer.add(
            GLD, now - 120 + i * 10, flight.latitude, flight.longitude,
            1300.0, 70.0, -1.0, 90.0,
        )

    result = await tracker.classify_flight_end(flight, tracker._configs["test"])
    assert result.scenario == "OUTLANDED"

    tracker.elevation = FakeElevation(None)
    result_af = await tracker.classify_flight_end(flight, tracker._configs["test"])
    assert result_af.scenario == "UNKNOWN"
