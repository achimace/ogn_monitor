"""Shared fixtures: synthetic beacons and a tracker-like simulator.

The simulator wires FlightStateMachine and LaunchDetector exactly like
FlightTracker does, but without Redis/PostgreSQL, so whole flights can be
replayed from synthetic beacon sequences.
"""

import time
from math import cos, radians

import pytest

from app.aprs.beacon_parser import Beacon
from app.tracking.flight_state import FlightStatus
from app.tracking.flight_state_machine import AirfieldConfig, FlightStateMachine
from app.tracking.launch_detector import LaunchDetector

AF_LAT = 47.64
AF_LON = 11.23
AF_ELEV = 660.0

# Beacon timestamps start one hour in the past so that wallclock based
# checks (check_timeouts) see them as "recent but not future".
T0 = time.time() - 3600


def make_config(**overrides) -> AirfieldConfig:
    kw = dict(id=1, slug="test", latitude=AF_LAT, longitude=AF_LON, elevation_m=AF_ELEV)
    kw.update(overrides)
    return AirfieldConfig(**kw)


def pos(east_m: float = 0.0, north_m: float = 0.0) -> tuple[float, float]:
    """Lat/lon of a point offset from the airfield reference by metres."""
    lat = AF_LAT + north_m / 111_320.0
    lon = AF_LON + east_m / (111_320.0 * cos(radians(AF_LAT)))
    return lat, lon


def beacon(fid: str, t: float, east: float = 0.0, north: float = 0.0,
           alt: float = AF_ELEV, speed: float = 0.0, vs: float = 0.0,
           track: float = 90.0, device_type: int = 0) -> Beacon:
    """Synthetic beacon ``t`` seconds after T0 at the given offset."""
    lat, lon = pos(east, north)
    return Beacon(
        timestamp=T0 + t,
        flarm_id=fid,
        lat=lat,
        lon=lon,
        altitude=alt,
        speed=speed,
        vs=vs,
        track=track,
        device_type=device_type,
    )


class Sim:
    """Minimal FlightTracker stand-in: state machine + launch detector."""

    def __init__(self, config: AirfieldConfig | None = None,
                 roles: dict[str, str] | None = None,
                 models: dict[str, str] | None = None,
                 registrations: dict[str, str] | None = None):
        self.config = config or make_config()
        self.sm = FlightStateMachine()
        self.ld = LaunchDetector()
        self.roles = roles or {}
        self.models = models or {}
        self.registrations = registrations or {}
        self.events: list[dict] = []

    def feed(self, b: Beacon):
        slug = self.config.slug
        existing = self.sm.get_flight(slug, b.flarm_id)
        old_status = existing.status if existing else None

        flight = self.sm.process_beacon(b, self.config)
        if flight is None:
            self.events += self.sm.drain_events()
            return None

        if not flight.registration:
            flight.registration = self.registrations.get(b.flarm_id, b.flarm_id)
            flight.aircraft_role = self.roles.get(b.flarm_id, "")
            flight.aircraft_model = self.models.get(b.flarm_id, "")

        airfield_flights = list(self.sm.get_all_flights(slug).values())
        is_new_takeoff = (
            flight.status == FlightStatus.TAKEOFF
            and (old_status is None or old_status == FlightStatus.LANDING)
        )
        if is_new_takeoff:
            if old_status == FlightStatus.LANDING:
                self.ld.cleanup(b.flarm_id)
            self.ld.on_takeoff(flight, airfield_flights, self.config)
        self.ld.on_beacon(flight, b, airfield_flights, self.config)

        self.events += self.sm.drain_events() + self.ld.drain_events()
        return flight

    def feed_all(self, beacons):
        last = None
        for b in beacons:
            last = self.feed(b) or last
        return last

    def timeouts(self):
        changed = self.sm.check_timeouts({self.config.slug: self.config})
        self.events += self.sm.drain_events()
        return changed

    def flight(self, fid: str):
        return self.sm.get_flight(self.config.slug, fid)

    def event_types(self, fid: str | None = None) -> list[str]:
        return [e["event_type"] for e in self.events
                if fid is None or e["flarm_id"] == fid]

    def events_of(self, etype: str, fid: str | None = None) -> list[dict]:
        return [e for e in self.events
                if e["event_type"] == etype and (fid is None or e["flarm_id"] == fid)]


# ---------------------------------------------------------------------------
# Reusable beacon sequences
# ---------------------------------------------------------------------------

def ground_roll(fid: str, t0: float = 0.0, device_type: int = 0) -> list[Beacon]:
    """Stationary, then accelerating, then airborne at 60 m AGL.

    Takeoff (lift-off beacon) is at t0+15, the ground roll starts at t0+9.
    """
    return [
        beacon(fid, t0 + 0, speed=0, device_type=device_type),
        beacon(fid, t0 + 3, speed=0, device_type=device_type),
        beacon(fid, t0 + 6, speed=5, device_type=device_type),
        beacon(fid, t0 + 9, east=20, speed=45, vs=0.0, device_type=device_type),
        beacon(fid, t0 + 12, east=70, alt=AF_ELEV + 15, speed=80, vs=2.0, device_type=device_type),
        beacon(fid, t0 + 15, east=150, alt=AF_ELEV + 60, speed=95, vs=3.0, device_type=device_type),
    ]


def fly_away(fid: str, t0: float, n: int = 3, alt: float = AF_ELEV + 400) -> list[Beacon]:
    """A few beacons well outside the home radius (FLYING)."""
    return [
        beacon(fid, t0 + i * 3, east=3000 + i * 80, alt=alt, speed=95, vs=0.0)
        for i in range(n)
    ]


def approach_and_land(fid: str, t0: float) -> list[Beacon]:
    """Final approach and rollout at home.

    Touchdown (start of the slow phase) is at t0+12, the landing is
    detected at t0+22 after the 10 s hysteresis.
    """
    return [
        beacon(fid, t0 + 0, east=300, alt=AF_ELEV + 40, speed=90, vs=-2.0),
        beacon(fid, t0 + 3, east=200, alt=AF_ELEV + 20, speed=80, vs=-1.5),
        beacon(fid, t0 + 6, east=100, alt=AF_ELEV + 5, speed=60, vs=-1.0),
        beacon(fid, t0 + 9, east=50, alt=AF_ELEV + 1, speed=45, vs=0.0),
        beacon(fid, t0 + 12, east=20, alt=AF_ELEV, speed=30, vs=0.0),
        beacon(fid, t0 + 15, east=10, alt=AF_ELEV, speed=15, vs=0.0),
        beacon(fid, t0 + 18, east=5, alt=AF_ELEV, speed=5, vs=0.0),
        beacon(fid, t0 + 22, east=0, alt=AF_ELEV, speed=0, vs=0.0),
    ]


@pytest.fixture
def sim() -> Sim:
    return Sim()
