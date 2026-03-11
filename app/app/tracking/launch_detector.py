"""Launch Type Detection - Winch, Aerotow, Self-Launch.

Detects the launch method within the first ~3 minutes after takeoff.
Also handles F-Schlepp (aerotow) pair tracking and release detection.

Detection methods:
- WINCH: VS > 8 m/s, short duration, abrupt VS drop on release
- AEROTOW: Two aircraft < 150m apart, similar alt/course, separation = release
- SELF-LAUNCH: Known motorglider type, moderate climb, no nearby aircraft
"""

import time
from dataclasses import dataclass, field

import structlog

from app.aprs.beacon_parser import Beacon
from app.tracking.flight_state import FlightState, FlightStatus
from app.tracking.geo_calc import haversine

log = structlog.get_logger()

# Detection thresholds
WINCH_MIN_VS = 8.0                # m/s minimum vertical speed
WINCH_MAX_DURATION_S = 90         # Max winch launch duration
WINCH_VS_DROP_THRESHOLD = 5.0     # m/s drop for release detection

AEROTOW_MAX_DISTANCE_M = 150     # Max distance between pair
AEROTOW_ALT_DIFF_M = 80          # Max altitude difference in pair
AEROTOW_SEPARATION_DIST_M = 200  # Distance for separation detection
AEROTOW_MIN_DURATION_S = 120     # Min 2 minutes for aerotow
AEROTOW_SPEED_MIN = 90           # km/h typical min tow speed
AEROTOW_SPEED_MAX = 150          # km/h typical max tow speed

DETECTION_WINDOW_S = 180          # 3 minutes after takeoff

# Known self-launcher aircraft types and models
SELF_LAUNCH_TYPES = {8}           # Type 8 = TMG/motorglider
SELF_LAUNCH_MODELS = {
    "arcus m", "arcus t", "ventus 2cm", "ventus 2ct", "ventus 3m", "ventus 3t",
    "dg-808", "dg-808c", "dg-1001m", "dg-1001t", "ash 26e", "eta",
    "nimbus 4dm", "nimbus 4dt", "antares 20e", "antares 23e",
    "duo discus xlt", "hph 304ms", "js3 rj",
    "sf 25", "sf25", "dimona", "super dimona", "falke",
    "g 109", "g109", "grob 109",
}


@dataclass
class _LaunchDetectionState:
    """Tracking state for launch type detection of a single flight."""
    flarm_id: str
    takeoff_time: float           # monotonic
    beacons: list[Beacon] = field(default_factory=list)
    is_resolved: bool = False
    launch_type: str = "unknown"

    # Winch tracking
    had_winch_vs: bool = False
    max_vs: float = 0.0

    # Aerotow tracking
    tow_plane_id: str = ""
    was_paired: bool = False
    last_paired_alt: float = 0.0
    last_paired_time: float = 0.0
    pair_start_time: float = 0.0


class LaunchDetector:
    """Detects launch types within the first 3 minutes after takeoff."""

    def __init__(self):
        self._pending: dict[str, _LaunchDetectionState] = {}

    def on_takeoff(self, flight: FlightState, all_flights: list[FlightState]) -> None:
        """Called when a TAKEOFF is detected. Starts launch detection."""
        state = _LaunchDetectionState(
            flarm_id=flight.flarm_id,
            takeoff_time=time.monotonic(),
        )
        self._pending[flight.flarm_id] = state

        # Quick check: Known self-launcher?
        if _is_known_self_launcher(flight):
            self._resolve(flight, "self", reason="Bekannter Motorsegler")

    def on_beacon(self, flight: FlightState, beacon: Beacon,
                  all_flights: list[FlightState]) -> None:
        """Called for every beacon of a flight in detection window."""
        state = self._pending.get(flight.flarm_id)
        if not state or state.is_resolved:
            return

        state.beacons.append(beacon)
        elapsed = time.monotonic() - state.takeoff_time

        # After detection window: final classification
        if elapsed > DETECTION_WINDOW_S:
            self._final_classification(flight, state, all_flights)
            return

        # --- Running analysis ---

        # Winch detection: High VS in first 90 seconds
        if elapsed < WINCH_MAX_DURATION_S:
            if beacon.vs > WINCH_MIN_VS:
                state.had_winch_vs = True
                state.max_vs = max(state.max_vs, beacon.vs)

            # Winch release: Abrupt VS drop
            if state.had_winch_vs and len(state.beacons) >= 2:
                prev_vs = state.beacons[-2].vs
                vs_drop = prev_vs - beacon.vs
                if vs_drop > WINCH_VS_DROP_THRESHOLD and prev_vs > WINCH_MIN_VS * 0.7:
                    self._resolve(
                        flight, "winch",
                        release_alt=beacon.altitude,
                        reason=f"Windenausklinken bei {beacon.altitude:.0f}m, "
                               f"VS-Drop {vs_drop:.1f}m/s",
                    )
                    return

        # Aerotow detection: Find nearby aircraft
        tow_candidate = self._find_tow_plane(beacon, flight, all_flights)
        if tow_candidate:
            if not state.was_paired:
                state.tow_plane_id = tow_candidate.flarm_id
                state.was_paired = True
                state.pair_start_time = time.monotonic()
                log.info(
                    "aerotow_pair_detected",
                    glider=flight.flarm_id,
                    tow=tow_candidate.flarm_id,
                )

            state.last_paired_alt = beacon.altitude
            state.last_paired_time = time.monotonic()

        # Aerotow separation detection
        if state.was_paired and state.tow_plane_id:
            tow_flight = _find_flight(state.tow_plane_id, all_flights)
            if tow_flight:
                dist = haversine(
                    beacon.lat, beacon.lon,
                    tow_flight.latitude, tow_flight.longitude,
                )
                alt_diff = abs(beacon.altitude - tow_flight.altitude_m)

                if dist > AEROTOW_SEPARATION_DIST_M or alt_diff > AEROTOW_ALT_DIFF_M * 2:
                    pair_duration = time.monotonic() - state.pair_start_time
                    if pair_duration > AEROTOW_MIN_DURATION_S:
                        self._resolve(
                            flight, "aerotow",
                            release_alt=state.last_paired_alt,
                            tow_plane_id=state.tow_plane_id,
                            tow_plane_reg=tow_flight.registration,
                            reason=f"Ausklinken bei {state.last_paired_alt:.0f}m, "
                                   f"Schleppzeit {pair_duration:.0f}s",
                        )
                        return

    def is_pending(self, flarm_id: str) -> bool:
        """Check if launch detection is still pending for a flight."""
        state = self._pending.get(flarm_id)
        return state is not None and not state.is_resolved

    def cleanup(self, flarm_id: str) -> None:
        """Remove detection state (after landing/archive)."""
        self._pending.pop(flarm_id, None)

    def _find_tow_plane(self, beacon: Beacon, flight: FlightState,
                        all_flights: list[FlightState]) -> FlightState | None:
        """Find a nearby aircraft that could be the tow plane."""
        for other in all_flights:
            if other.flarm_id == flight.flarm_id:
                continue
            if other.status not in (FlightStatus.TAKEOFF, FlightStatus.FLYING, FlightStatus.TOWING):
                continue

            dist = haversine(beacon.lat, beacon.lon, other.latitude, other.longitude)
            alt_diff = abs(beacon.altitude - other.altitude_m)

            if (dist < AEROTOW_MAX_DISTANCE_M
                    and alt_diff < AEROTOW_ALT_DIFF_M
                    and AEROTOW_SPEED_MIN <= other.speed_kmh <= AEROTOW_SPEED_MAX):
                return other

        return None

    def _final_classification(self, flight: FlightState, state: _LaunchDetectionState,
                              all_flights: list[FlightState]) -> None:
        """Final launch type decision after detection window expires."""
        if state.had_winch_vs and not state.was_paired:
            self._resolve(flight, "winch",
                          reason=f"Windenstart-Profil (max VS {state.max_vs:.1f}m/s)")
        elif state.was_paired:
            self._resolve(flight, "aerotow",
                          tow_plane_id=state.tow_plane_id,
                          reason="F-Schlepp erkannt (Paar-Tracking)")
        elif _is_known_self_launcher(flight):
            self._resolve(flight, "self", reason="Bekannter Motorsegler")
        else:
            self._resolve(flight, "unknown", reason="Startart nicht erkannt")

    def _resolve(self, flight: FlightState, launch_type: str,
                 release_alt: float = 0.0,
                 tow_plane_id: str = "",
                 tow_plane_reg: str = "",
                 reason: str = "") -> None:
        """Finalize launch type detection."""
        state = self._pending.get(flight.flarm_id)
        if state:
            state.is_resolved = True
            state.launch_type = launch_type

        flight.launch_type = launch_type
        if release_alt:
            flight.release_alt_m = release_alt
        if tow_plane_id:
            flight.tow_plane_flarm_id = tow_plane_id
        if tow_plane_reg:
            flight.tow_plane_reg = tow_plane_reg

        log.info(
            "launch_type_detected",
            flarm_id=flight.flarm_id,
            launch_type=launch_type,
            reason=reason,
        )


def _is_known_self_launcher(flight: FlightState) -> bool:
    """Check if the aircraft is a known self-launcher."""
    if not flight.aircraft_model:
        return False
    model_lower = flight.aircraft_model.lower().strip()
    return model_lower in SELF_LAUNCH_MODELS


def _find_flight(flarm_id: str, all_flights: list[FlightState]) -> FlightState | None:
    """Find a flight by FLARM ID in the active flights list."""
    for f in all_flights:
        if f.flarm_id == flarm_id:
            return f
    return None
