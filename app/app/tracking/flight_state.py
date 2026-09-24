"""Flight state model and status constants.

Defines the FlightState dataclass used throughout the tracking system.
This is the in-memory representation of a tracked flight.
"""

from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum

# Rolling window size for speed smoothing (matches PyAcphFlightsLogbook approach)
SPEED_WINDOW_SIZE = 3


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

    # Aircraft role from the tenant fleet / airfield config
    # (towplane / glider / motorglider_sl / powered / "")
    aircraft_role: str = ""
    # FLARM aircraft category from the beacon id byte
    # (1 glider/motorglider, 2 tow plane, 8 powered, 9 jet, 0 unknown)
    flarm_aircraft_type: int = 0

    # Launch detection
    launch_type: str = "unknown"  # winch / aerotow / aerotow_ambiguous / self / powered / unknown
    tow_plane_flarm_id: str = ""
    tow_plane_reg: str = ""
    release_alt_m: float = 0.0        # MSL, kept for display compatibility
    release_alt_agl_m: float = 0.0    # AGL = MSL - airfield elevation (billing)
    release_time: str = ""
    release_method: str = ""          # pair_separation / towplane_max / winch_vs_drop / winch_profile
    tow_duration_s: int = 0
    pairing_confidence: float = 0.0   # 0..1, only meaningful for aerotow

    # Landing bookkeeping (VF semantics: one flight, N landings)
    landing_count: int = 1
    landing_method: str = ""          # observed / silence
    landing_confidence: float = 0.0   # 0..1
    touch_go_confidence: float = 0.0  # confidence of the most recent touch & go
    landing_final: bool = False       # True once the landing can no longer become a T&G

    # Outlanding tracking
    outlanding_pending_since: float = 0.0  # monotonic timestamp

    # ----- Runtime-only fields (not persisted to Redis) -----

    # Speed hysteresis: beacon timestamp when the smoothed speed first
    # dropped below the landing threshold while near the ground.
    _slow_since: float = 0.0

    # Beacon timestamp of the most recent beacon (for beacon-time deltas)
    _last_beacon_ts: float = 0.0

    # Beacon timestamp of the touchdown that produced the current LANDING
    _landing_ts: float = 0.0

    # Extremes observed while on the ground after landing (T&G confidence)
    _ground_min_agl: float = 0.0
    _ground_min_speed: float = 0.0

    # Silence-landing candidate: last beacon that looked like a final
    # approach at home (low, slow, sinking). 0 = no candidate.
    # Not persisted: after a worker restart an aircraft on final falls
    # back to the SIGNAL_LOST path instead of a silence landing.
    _silence_candidate_ts: float = 0.0
    _silence_candidate_conf: float = 0.0

    # Ground roll start while sticky-landed (restart takeoff time)
    _restart_fast_since_ts: float = 0.0

    # Consecutive beacons dropped as out-of-order (timeline reset guard)
    _ooo_drops: int = 0

    # Rolling window of recent ground speeds for noise-resistant detection
    _recent_speeds: deque = field(
        default_factory=lambda: deque(maxlen=SPEED_WINDOW_SIZE)
    )

    def push_speed(self, speed_kmh: float) -> float:
        """Append a speed sample and return the rolling average."""
        self._recent_speeds.append(speed_kmh)
        return sum(self._recent_speeds) / len(self._recent_speeds)

    def reset_speed_window(self) -> None:
        """Clear the rolling speed window (after a status change)."""
        self._recent_speeds.clear()
        self._slow_since = 0.0

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
            "aircraft_role": self.aircraft_role,
            "flarm_aircraft_type": str(self.flarm_aircraft_type),
            "launch_type": self.launch_type,
            "tow_plane_flarm_id": self.tow_plane_flarm_id,
            "tow_plane_reg": self.tow_plane_reg,
            "release_alt_m": str(round(self.release_alt_m)),
            "release_alt_agl_m": str(round(self.release_alt_agl_m)),
            "release_time": self.release_time,
            "release_method": self.release_method,
            "tow_duration_s": str(self.tow_duration_s),
            "pairing_confidence": str(round(self.pairing_confidence, 2)),
            "landing_count": str(self.landing_count),
            "landing_method": self.landing_method,
            "landing_confidence": str(round(self.landing_confidence, 2)),
            "touch_go_confidence": str(round(self.touch_go_confidence, 2)),
            "landing_final": "1" if self.landing_final else "0",
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
            aircraft_role=data.get("aircraft_role", ""),
            flarm_aircraft_type=int(float(data.get("flarm_aircraft_type", "0"))),
            launch_type=data.get("launch_type", "unknown"),
            tow_plane_flarm_id=data.get("tow_plane_flarm_id", ""),
            tow_plane_reg=data.get("tow_plane_reg", ""),
            release_alt_m=float(data.get("release_alt_m", "0")),
            release_alt_agl_m=float(data.get("release_alt_agl_m", "0")),
            release_time=data.get("release_time", ""),
            release_method=data.get("release_method", ""),
            tow_duration_s=int(float(data.get("tow_duration_s", "0"))),
            pairing_confidence=float(data.get("pairing_confidence", "0")),
            landing_count=int(float(data.get("landing_count", "1"))),
            landing_method=data.get("landing_method", ""),
            landing_confidence=float(data.get("landing_confidence", "0")),
            touch_go_confidence=float(data.get("touch_go_confidence", "0")),
            landing_final=data.get("landing_final", "0") == "1",
        )
