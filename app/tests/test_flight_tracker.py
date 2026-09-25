"""FlightTracker wiring: events, archiving and detector lifecycle.

Uses an in-memory RedisWriter stand-in; PostgreSQL archiving is stubbed.
"""

from unittest.mock import AsyncMock

import pytest

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

    async def update_flight(self, slug, fid, data, ttl=None):
        self.flights[(slug, fid)] = data

    async def publish_beacon(self, slug, fid, data):
        pass

    async def add_position(self, slug, fid, *args):
        pass

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
