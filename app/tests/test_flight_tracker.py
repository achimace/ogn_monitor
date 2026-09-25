"""FlightTracker wiring: events, archiving and detector lifecycle.

Uses an in-memory RedisWriter stand-in; PostgreSQL archiving is stubbed.
"""

from unittest.mock import AsyncMock

import pytest

from app.config import settings
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

    async def update_flight(self, slug, fid, data, ttl=None):
        self.flights[(slug, fid)] = data

    async def publish_beacon(self, slug, fid, data):
        pass

    async def add_position(self, slug, fid, *args):
        pass

    async def add_track_point(self, slug, fid, ts_ms, *args, retention_s=0, min_interval_s=0):
        self.track_points.append((slug, fid, ts_ms))

    async def publish_event(self, slug, etype, fid, data=None, message=""):
        self.events.append((slug, etype, fid))

    async def remove_flight(self, slug, fid):
        self.flights.pop((slug, fid), None)

    async def get_active_flights(self, slug):
        return {fid for (s, fid) in self.flights if s == slug}

    async def get_flight(self, slug, fid):
        return self.flights.get((slug, fid))


class FakeResolver:
    def resolve(self, flarm_id):
        return None

    def update_from_aprs(self, flarm_id, registration):
        pass


@pytest.fixture
def tracker():
    t = FlightTracker(FakeRedisWriter(), FakeResolver())
    t.set_configs({"test": make_config()})
    t._archive_to_log = AsyncMock()
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
