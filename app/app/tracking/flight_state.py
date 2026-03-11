"""Flight state model and status constants.

Defines the FlightState dataclass used throughout the tracking system.
This is the in-memory representation of a tracked flight.
"""

from dataclasses import dataclass, field
from enum import IntEnum


class FlightStatus(IntEnum):
    """Flight status values for the state machine."""
    GROUND = 0
    TAKEOFF = 1
    FLYING = 2
    LANDING = 3
    OUTLANDING = 4
    ALARM = 5
    TOWING = 6
    OUTLANDING_PENDING = 7
    EMERGENCY = 8
    DIVERTED = 9
    SIGNAL_LOST = 10


# Statuses that indicate an airborne flight
AIRBORNE_STATUSES = frozenset({
    FlightStatus.TAKEOFF,
    FlightStatus.FLYING,
    FlightStatus.TOWING,
    FlightStatus.SIGNAL_LOST,
})

# Statuses that trigger immediate PG write
EVENT_STATUSES = frozenset({
    FlightStatus.TAKEOFF,
    FlightStatus.LANDING,
    FlightStatus.ALARM,
    FlightStatus.OUTLANDING,
    FlightStatus.EMERGENCY,
    FlightStatus.DIVERTED,
})


@dataclass
class FlightState:
    """In-memory state of a tracked flight."""

    # Identity
    flarm_id: str
    airfield_slug: str
    airfield_id: int = 0

    # Aircraft info (from resolver)
    registration: str = ""
    aircraft_model: str = ""
    competition_sign: str = ""

    # Status
    status: FlightStatus = FlightStatus.GROUND

    # Position
    latitude: float = 0.0
    longitude: float = 0.0
    altitude_m: float = 0.0
    altitude_agl: float = 0.0
    speed_kmh: float = 0.0
    vertical_speed_ms: float = 0.0
    track_deg: float = 0.0

    # Relative to airfield
    distance_m: float = 0.0
    qdr_deg: float = 0.0
    bearing_text: str = ""

    # Timing
    takeoff_time: str = ""
    landing_time: str = ""
    last_seen: str = ""
    elapsed_s: int = 0

    # Extremes
    max_altitude_m: float = 0.0
    max_distance_m: float = 0.0

    # Receiver
    receiver: str = ""

    # Launch detection
    launch_type: str = "unknown"  # winch / aerotow / self / unknown
    tow_plane_flarm_id: str = ""
    tow_plane_reg: str = ""
    release_alt_m: float = 0.0
    release_time: str = ""

    # Outlanding tracking
    outlanding_pending_since: float = 0.0  # monotonic timestamp

    # Speed hysteresis tracking
    _slow_since: float = 0.0  # monotonic time when speed dropped below threshold

    def to_redis_dict(self) -> dict[str, str]:
        """Convert to dict suitable for Redis HSET."""
        return {
            "flarm_id": self.flarm_id,
            "registration": self.registration,
            "aircraft_model": self.aircraft_model,
            "competition_sign": self.competition_sign,
            "status": str(self.status.value),
            "latitude": str(round(self.latitude, 5)),
            "longitude": str(round(self.longitude, 5)),
            "altitude_m": str(round(self.altitude_m)),
            "altitude_agl": str(round(self.altitude_agl)),
            "speed_kmh": str(round(self.speed_kmh)),
            "vertical_speed_ms": str(round(self.vertical_speed_ms, 1)),
            "track_deg": str(round(self.track_deg)),
            "distance_m": str(round(self.distance_m)),
            "qdr_deg": str(round(self.qdr_deg)),
            "bearing_text": self.bearing_text,
            "takeoff_time": self.takeoff_time,
            "landing_time": self.landing_time,
            "last_seen": self.last_seen,
            "elapsed_s": str(self.elapsed_s),
            "max_altitude_m": str(round(self.max_altitude_m)),
            "max_distance_m": str(round(self.max_distance_m)),
            "receiver": self.receiver,
            "launch_type": self.launch_type,
            "tow_plane_flarm_id": self.tow_plane_flarm_id,
            "tow_plane_reg": self.tow_plane_reg,
            "release_alt_m": str(round(self.release_alt_m)),
            "release_time": self.release_time,
        }

    @classmethod
    def from_redis(cls, data: dict[str, str], airfield_slug: str = "") -> "FlightState":
        """Restore FlightState from Redis hash data."""
        return cls(
            flarm_id=data.get("flarm_id", ""),
            airfield_slug=airfield_slug,
            registration=data.get("registration", ""),
            aircraft_model=data.get("aircraft_model", ""),
            competition_sign=data.get("competition_sign", ""),
            status=FlightStatus(int(data.get("status", "0"))),
            latitude=float(data.get("latitude", "0")),
            longitude=float(data.get("longitude", "0")),
            altitude_m=float(data.get("altitude_m", "0")),
            altitude_agl=float(data.get("altitude_agl", "0")),
            speed_kmh=float(data.get("speed_kmh", "0")),
            vertical_speed_ms=float(data.get("vertical_speed_ms", "0")),
            track_deg=float(data.get("track_deg", "0")),
            distance_m=float(data.get("distance_m", "0")),
            qdr_deg=float(data.get("qdr_deg", "0")),
            bearing_text=data.get("bearing_text", ""),
            takeoff_time=data.get("takeoff_time", ""),
            landing_time=data.get("landing_time", ""),
            last_seen=data.get("last_seen", ""),
            elapsed_s=int(data.get("elapsed_s", "0")),
            max_altitude_m=float(data.get("max_altitude_m", "0")),
            max_distance_m=float(data.get("max_distance_m", "0")),
            receiver=data.get("receiver", ""),
            launch_type=data.get("launch_type", "unknown"),
            tow_plane_flarm_id=data.get("tow_plane_flarm_id", ""),
            tow_plane_reg=data.get("tow_plane_reg", ""),
            release_alt_m=float(data.get("release_alt_m", "0")),
            release_time=data.get("release_time", ""),
        )
