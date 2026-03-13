"""Flight State Machine - processes beacons and manages flight status transitions.

Handles:
- Takeoff detection (aircraft leaves home airfield)
- Landing detection at home (speed + hysteresis)
- Outlanding detection (slow + low away from home)
- Alarm on signal loss (timeout)
- Status transitions with event publishing

Configurable per airfield via AirfieldConfig.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from app.aprs.beacon_parser import Beacon
from app.tracking.flight_state import FlightState, FlightStatus, AIRBORNE_STATUSES
from app.tracking.geo_calc import azimuth, degrees_to_compass, haversine, altitude_agl

log = structlog.get_logger()


@dataclass
class AirfieldConfig:
    """Per-airfield configuration for flight detection."""
    id: int
    slug: str
    latitude: float
    longitude: float
    elevation_m: float
    home_radius_m: int = 800
    takeoff_speed_kmh: int = 40
    takeoff_alt_offset_m: int = 50
    takeoff_max_agl_m: int = 1000
    landing_speed_kmh: int = 50
    alarm_timeout_s: int = 600
    signal_loss_timeout_s: int = 300
    outlanding_timeout_s: int = 300
    hysteresis_s: int = 10
    ogn_filter_radius_km: int = 500
    tow_plane_flarm_ids: list[str] | None = None
    winch_vs_threshold_ms: float = 8.0
    ground_speed_max_kmh: int = 30
    ground_max_agl_m: int = 50


def _utcnow_iso() -> str:
    """Current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FlightStateMachine:
    """Processes beacons and manages flight state transitions."""

    # Max age for ground cache entries (seconds, monotonic)
    GROUND_CACHE_MAX_AGE_S = 7200  # 2 hours

    def __init__(self):
        # Active flights: airfield_slug -> { flarm_id -> FlightState }
        self.flights: dict[str, dict[str, FlightState]] = {}

        # Ground cache: tracks aircraft seen stationary at home airfield
        # airfield_slug -> { flarm_id -> monotonic timestamp first seen on ground }
        self._ground_cache: dict[str, dict[str, float]] = {}

        # Events generated during processing (consumed by flight tracker)
        self._pending_events: list[dict] = []

    def get_flight(self, airfield_slug: str, flarm_id: str) -> FlightState | None:
        """Get a flight by airfield and FLARM ID."""
        return self.flights.get(airfield_slug, {}).get(flarm_id)

    def get_all_flights(self, airfield_slug: str) -> dict[str, FlightState]:
        """Get all active flights for an airfield."""
        return self.flights.get(airfield_slug, {})

    def get_all_active_flights(self) -> list[FlightState]:
        """Get all active flights across all airfields."""
        result = []
        for af_flights in self.flights.values():
            result.extend(af_flights.values())
        return result

    def drain_events(self) -> list[dict]:
        """Get and clear pending events."""
        events = self._pending_events
        self._pending_events = []
        return events

    def process_beacon(self, beacon: Beacon, config: AirfieldConfig) -> FlightState | None:
        """Process a beacon for a specific airfield.

        Either updates an existing flight or detects a new takeoff.

        Returns:
            Updated FlightState, or None if beacon was discarded.
        """
        slug = config.slug
        flight = self.get_flight(slug, beacon.flarm_id)

        # Calculate position relative to airfield
        dist_m = haversine(beacon.lat, beacon.lon, config.latitude, config.longitude)
        qdr = azimuth(config.latitude, config.longitude, beacon.lat, beacon.lon)
        agl = altitude_agl(beacon.altitude, config.elevation_m)
        at_home = dist_m <= config.home_radius_m
        is_slow = beacon.speed < config.landing_speed_kmh

        now_mono = time.monotonic()
        now_iso = _utcnow_iso()

        if flight is None:
            # Not tracking this aircraft yet - check for takeoff
            if not at_home:
                return None  # Not at home airfield, discard

            # Ensure ground cache dict exists for this airfield
            if slug not in self._ground_cache:
                self._ground_cache[slug] = {}
            gc = self._ground_cache[slug]

            # Track aircraft seen stationary on the ground
            is_on_ground = (beacon.speed < config.ground_speed_max_kmh
                            and agl < config.ground_max_agl_m)
            if is_on_ground:
                if beacon.flarm_id not in gc:
                    gc[beacon.flarm_id] = now_mono
                    log.debug(
                        "ground_contact",
                        flarm_id=beacon.flarm_id,
                        airfield=slug,
                        speed=beacon.speed,
                        agl=round(agl),
                    )
                return None  # On ground, not airborne yet

            # Aircraft is moving/airborne - check for takeoff
            is_high = beacon.altitude > config.elevation_m + config.takeoff_alt_offset_m
            is_too_high = agl > config.takeoff_max_agl_m
            is_fast = beacon.speed >= config.takeoff_speed_kmh
            was_on_ground = beacon.flarm_id in gc

            if is_high and is_fast and not is_too_high and was_on_ground:
                # Aircraft was on ground and is now airborne - takeoff!
                gc.pop(beacon.flarm_id, None)
                flight = FlightState(
                    flarm_id=beacon.flarm_id,
                    airfield_slug=slug,
                    airfield_id=config.id,
                    status=FlightStatus.TAKEOFF,
                    takeoff_time=now_iso,
                )
                if slug not in self.flights:
                    self.flights[slug] = {}
                self.flights[slug][beacon.flarm_id] = flight

                self._emit_event(slug, "takeoff", beacon.flarm_id, flight)
                log.info(
                    "takeoff_detected",
                    flarm_id=beacon.flarm_id,
                    airfield=slug,
                    altitude=beacon.altitude,
                    speed=beacon.speed,
                )
            elif not was_on_ground:
                log.debug(
                    "overflight_ignored",
                    flarm_id=beacon.flarm_id,
                    airfield=slug,
                    agl=round(agl),
                    speed=beacon.speed,
                    reason="not_seen_on_ground",
                )
                return None  # Never seen on ground - overflight
            else:
                return None  # At home but not yet taking off

        # Update flight position data
        flight.latitude = beacon.lat
        flight.longitude = beacon.lon
        flight.altitude_m = beacon.altitude
        flight.altitude_agl = agl
        flight.speed_kmh = beacon.speed
        flight.vertical_speed_ms = beacon.vs
        flight.track_deg = beacon.track
        flight.distance_m = dist_m
        flight.qdr_deg = round(qdr, 1)
        flight.bearing_text = degrees_to_compass(qdr)
        flight.last_seen = now_iso
        flight.elapsed_s = 0
        flight.receiver = beacon.receiver

        # Update extremes
        flight.max_altitude_m = max(flight.max_altitude_m, beacon.altitude)
        flight.max_distance_m = max(flight.max_distance_m, dist_m)

        old_status = flight.status

        # --- State transitions ---

        # Landing detection at home airfield
        if at_home and is_slow and flight.status in AIRBORNE_STATUSES:
            if flight._slow_since == 0:
                flight._slow_since = now_mono
            elif (now_mono - flight._slow_since) >= config.hysteresis_s:
                flight.status = FlightStatus.LANDING
                flight.landing_time = now_iso
                self._emit_event(
                    slug, "landing", beacon.flarm_id, flight,
                    message=f"Landung am Heimatplatz",
                )
                log.info(
                    "landing_detected",
                    flarm_id=beacon.flarm_id,
                    airfield=slug,
                    flight_time=flight.takeoff_time,
                )
        else:
            flight._slow_since = 0.0

        # Transition from TAKEOFF to FLYING
        if flight.status == FlightStatus.TAKEOFF and not at_home:
            flight.status = FlightStatus.FLYING

        # Signal recovered (was in ALARM/SIGNAL_LOST)
        if flight.status in (FlightStatus.ALARM, FlightStatus.SIGNAL_LOST):
            flight.status = FlightStatus.FLYING
            self._emit_event(
                slug, "signal_recovered", beacon.flarm_id, flight,
                message=f"Signal wieder da: {dist_m/1000:.1f}km {degrees_to_compass(qdr)}, {beacon.altitude:.0f}m",
            )
            log.info("signal_recovered", flarm_id=beacon.flarm_id, airfield=slug)

        # Outlanding detection: slow and low, away from home
        if (flight.status in (FlightStatus.FLYING, FlightStatus.TAKEOFF)
                and not at_home and is_slow and agl < 150):
            flight.status = FlightStatus.OUTLANDING_PENDING
            flight.outlanding_pending_since = now_mono

        # Outlanding recovery: speed picked up again
        if flight.status == FlightStatus.OUTLANDING_PENDING:
            if not is_slow or agl > 250:
                flight.status = FlightStatus.FLYING
                flight.outlanding_pending_since = 0

        if flight.status != old_status:
            log.debug(
                "status_change",
                flarm_id=beacon.flarm_id,
                old=old_status.name,
                new=flight.status.name,
            )

        return flight

    def check_timeouts(self, configs: dict[str, AirfieldConfig]) -> list[FlightState]:
        """Check all active flights for signal loss timeouts.

        Called periodically (every ~30s) by the worker.

        Returns:
            List of flights whose status changed.
        """
        changed = []
        now_mono = time.monotonic()

        for slug, af_flights in list(self.flights.items()):
            config = configs.get(slug)
            if not config:
                continue

            for flarm_id, flight in list(af_flights.items()):
                if flight.status == FlightStatus.LANDING:
                    continue

                # Calculate elapsed since last beacon
                # (We use the stored ISO time, but for timeout checks
                #  we need monotonic comparison. elapsed_s is updated
                #  on each beacon, so we increment it here.)
                if flight.elapsed_s == 0 and flight.last_seen:
                    # Was just updated by a beacon, skip
                    continue

                flight.elapsed_s += 30  # Approximate increment

                # ALARM: No signal for alarm_timeout_s
                if (flight.elapsed_s > config.alarm_timeout_s
                        and flight.status in AIRBORNE_STATUSES):
                    flight.status = FlightStatus.ALARM
                    self._emit_event(
                        slug, "alarm", flarm_id, flight,
                        message=(
                            f"Kein Signal seit {flight.elapsed_s // 60} Min. "
                            f"Letzte Position: {flight.distance_m/1000:.1f}km "
                            f"{flight.bearing_text}, {flight.altitude_m:.0f}m, "
                            f"QDR {flight.qdr_deg:.0f}°"
                        ),
                    )
                    log.warning(
                        "alarm_triggered",
                        flarm_id=flarm_id,
                        airfield=slug,
                        elapsed_s=flight.elapsed_s,
                    )
                    changed.append(flight)

                # OUTLANDING: Sitting still for outlanding_timeout_s
                if flight.status == FlightStatus.OUTLANDING_PENDING:
                    pending_elapsed = now_mono - flight.outlanding_pending_since
                    if pending_elapsed > config.outlanding_timeout_s:
                        flight.status = FlightStatus.OUTLANDING
                        self._emit_event(
                            slug, "outlanding", flarm_id, flight,
                            message=(
                                f"Aussenlandung: {flight.distance_m/1000:.0f}km "
                                f"{flight.bearing_text}, Position: "
                                f"{flight.latitude:.4f}°N {flight.longitude:.4f}°E"
                            ),
                        )
                        log.warning(
                            "outlanding_detected",
                            flarm_id=flarm_id,
                            airfield=slug,
                        )
                        changed.append(flight)

        # Clean up stale ground cache entries (> 2h old)
        for slug, gc in list(self._ground_cache.items()):
            stale = [fid for fid, ts in gc.items()
                     if (now_mono - ts) > self.GROUND_CACHE_MAX_AGE_S]
            for fid in stale:
                del gc[fid]

        return changed

    def archive_flight(self, airfield_slug: str, flarm_id: str) -> FlightState | None:
        """Remove a flight from active tracking (after landing/archive).

        Returns the archived flight state or None.
        """
        af_flights = self.flights.get(airfield_slug)
        if not af_flights:
            return None
        return af_flights.pop(flarm_id, None)

    def restore_flight(self, airfield_slug: str, flight: FlightState) -> None:
        """Restore a flight from Redis (on worker restart)."""
        if airfield_slug not in self.flights:
            self.flights[airfield_slug] = {}
        self.flights[airfield_slug][flight.flarm_id] = flight

    def _emit_event(self, airfield_slug: str, event_type: str, flarm_id: str,
                    flight: FlightState, message: str = "") -> None:
        """Queue an event for the flight tracker to publish."""
        self._pending_events.append({
            "airfield_slug": airfield_slug,
            "event_type": event_type,
            "flarm_id": flarm_id,
            "flight": flight,
            "message": message,
        })
