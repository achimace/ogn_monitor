"""Flight State Machine - processes beacons and manages flight status transitions.

Handles:
- Takeoff detection (aircraft leaves home airfield)
- Landing detection at home (speed + hysteresis)
- Touch & Go: re-takeoff shortly after landing continues the SAME flight
  (landing_count++), a final landing is confirmed by ``landing_final``
- Silence landing: final approach at home followed by radio silence
- Outlanding detection (slow + low away from home)
- Alarm on signal loss (timeout)
- Status transitions with event publishing

All flight-phase decisions are made on *beacon* timestamps so that delayed
APRS delivery does not distort takeoff/landing times and so that the whole
machine can be replayed deterministically in tests. Wallclock time is only
used for "no beacon received for N seconds" style timeouts.

Height above ground - two references
------------------------------------
``process_beacon`` receives an optional ``terrain_m`` (ground elevation
under the aircraft from the terrain model, see ``tracking/elevation.py``)
and keeps two AGL values:

- ``agl_af`` = altitude - airfield elevation. Used for every decision that
  is tied to the home runway: ground contact before takeoff, takeoff
  "too high" guard, silence-landing candidate, restart / touch & go
  confidence (``_ground_min_agl``). The runway elevation is what matters
  there, and the DSM cell over the airfield may carry hangar/tree noise.
  ``is_high`` and the landing band ``near_ground`` are explicitly
  airfield-relative as well.
- ``agl`` = altitude - terrain (falls back to ``agl_af`` when the terrain
  is unknown or the feature is disabled). Used for everything that
  describes the aircraft relative to the ground *wherever it is*:
  ``FlightState.altitude_agl`` (display, track stream, flight_log),
  outlanding detection / recovery and the "clearly airborne anywhere"
  check that retracts a phantom silence landing.

Configurable per airfield via AirfieldConfig.
"""

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from app.aprs.beacon_parser import Beacon
from app.tracking.flight_state import (
    AIRBORNE_STATUSES,
    SPEED_WINDOW_SIZE,
    FlightState,
    FlightStatus,
)
from app.tracking.geo_calc import azimuth, degrees_to_compass, haversine, altitude_agl

log = structlog.get_logger()

# Silence-landing candidate: a beacon counts as "on final at home" when the
# vertical speed is at most this (m/s) and the ground speed is below
# landing_speed_kmh + this margin (km/h).
SILENCE_MAX_VS_MS = 0.5
SILENCE_APPROACH_SPEED_MARGIN_KMH = 40

# Touch & go confidence thresholds
TG_CONF_MIN_AGL_M = 20          # ground roll seen this low -> +0.2
TG_CONF_SPEED_BELOW_LANDING = 10  # min speed < landing_speed - 10 -> +0.2
TG_CONF_MIN_GROUND_S = 30       # at least this long on the ground -> +0.1

# Beacons older than the flight's last beacon by more than this are
# dropped (out-of-order delivery, midnight rollover in the parser). If
# several consecutive beacons are "behind", the reference itself was wrong
# (a receiver with a clock running ahead): switch to the new timeline.
OUT_OF_ORDER_TOLERANCE_S = 60
OUT_OF_ORDER_MAX_DROPS = 3


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
    # Takeoff time = first beacon at/above takeoff speed, as long as the
    # ground roll took no longer than this. Otherwise the lift-off beacon.
    takeoff_roll_max_s: int = 90
    # Takeoff / restart / touch & go is only declared once the RAW ground
    # speed has been at/above takeoff_speed_kmh on this many consecutive
    # beacons (declaring beacon included). A single GPS glitch beacon
    # (speed + altitude jump) can otherwise pass the rolling-average and
    # altitude checks on its own. The takeoff time is unaffected (first
    # fast beacon); only the declaration waits. <= 1 = legacy behaviour.
    # Default from settings.takeoff_min_fast_beacons (worker).
    takeoff_min_fast_beacons: int = 2
    landing_speed_kmh: int = 50
    # Two-stage absence escalation:
    #   signal_loss_timeout_s → yellow "SIGNAL_LOST" (harmless, just info)
    #   alarm_timeout_s       → red "ALARM" (real action required)
    signal_loss_timeout_s: int = 300   # 5 min
    alarm_timeout_s: int = 7200        # 2 h
    outlanding_timeout_s: int = 300
    # Sticky landed: flights stay in LANDING state until either the same
    # aircraft starts again, OR this many seconds elapse (safety cleanup).
    sticky_landed_max_age_s: int = 86400  # 24 h
    hysteresis_s: int = 10
    # Touch & Go: a re-takeoff within this many seconds after touchdown
    # continues the same flight (landing_count++). Later = new flight.
    touch_go_max_ground_s: int = 90
    # Re-takeoff sooner than this after a detected landing is a bounce /
    # mis-detection: the landing is retracted and NOT counted.
    bounce_debounce_s: int = 15
    # Silence landing: final approach at home, then no beacon for this long
    # -> landing with landing_method='silence'. Must be < signal_loss_timeout_s
    # to take precedence over the SIGNAL_LOST escalation.
    silence_landing_s: int = 180
    ogn_filter_radius_km: int = 500
    tow_plane_flarm_ids: list[str] | None = None
    winch_vs_threshold_ms: float = 8.0
    ground_speed_max_kmh: int = 30
    ground_max_agl_m: int = 50
    # Altitude band around airfield elevation considered "near ground" (m)
    near_ground_band_m: int = 60
    # Outlanding suspicion: slow and below this AGL (terrain model when
    # available) away from home; recovered when climbing above the second.
    outlanding_max_agl_m: int = 150
    outlanding_recover_agl_m: int = 250
    # Optional shapely Polygon (lon/lat, EPSG:4326). When set, this defines
    # the "home area" precisely; the circular home_radius_m is then unused.
    # Typed as Any so this module does not hard-import shapely.
    home_polygon: Any = None


def _utcnow_iso() -> str:
    """Current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_ts(ts: float) -> str:
    """Unix timestamp -> ISO 8601 UTC string (second precision)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_from_iso(iso: str) -> float:
    """ISO 8601 string -> unix timestamp. Returns 0.0 if unparseable."""
    if not iso:
        return 0.0
    try:
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _seconds_since_iso(iso: str) -> float | None:
    """Wallclock seconds elapsed since the given ISO timestamp.

    Returns None if the input is empty or unparseable.
    """
    ts = _ts_from_iso(iso)
    if not ts:
        return None
    return time.time() - ts


class FlightStateMachine:
    """Processes beacons and manages flight state transitions."""

    # Max age for ground cache entries (seconds, monotonic)
    GROUND_CACHE_MAX_AGE_S = 7200  # 2 hours

    def __init__(self):
        # Active flights: airfield_slug -> { flarm_id -> FlightState }
        self.flights: dict[str, dict[str, FlightState]] = {}

        # Ground cache: tracks aircraft seen stationary at home airfield.
        # airfield_slug -> { flarm_id -> {"first_seen": mono_ts,
        #                                 "speeds": deque,
        #                                 "fast_since_ts": beacon_ts} }
        # The speed deque smooths the takeoff decision over the last N beacons
        # so a single GPS speed glitch cannot trigger a false takeoff.
        # fast_since_ts remembers when the ground roll started so the takeoff
        # time is the start of the roll, not the moment we notice 50 m AGL.
        self._ground_cache: dict[str, dict[str, dict]] = {}

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

    def requeue_events(self, events: list[dict]) -> None:
        """Put drained events back (in front), e.g. after filtering."""
        if events:
            self._pending_events = events + self._pending_events

    def process_beacon(self, beacon: Beacon, config: AirfieldConfig,
                       terrain_m: float | None = None) -> FlightState | None:
        """Process a beacon for a specific airfield.

        Either updates an existing flight or detects a new takeoff.

        Args:
            beacon: Parsed APRS beacon.
            config: Airfield configuration.
            terrain_m: Ground elevation (m MSL) under the beacon position
                from the terrain model, or None (unknown / disabled) to
                use the airfield elevation. See the module docstring for
                which decisions use which reference.

        Returns:
            Updated FlightState, or None if beacon was discarded.
        """
        slug = config.slug
        flight = self.get_flight(slug, beacon.flarm_id)

        # Calculate position relative to airfield
        dist_m = haversine(beacon.lat, beacon.lon, config.latitude, config.longitude)
        qdr = azimuth(config.latitude, config.longitude, beacon.lat, beacon.lon)
        # Airfield-relative AGL (runway decisions) vs terrain AGL (generic)
        agl_af = altitude_agl(beacon.altitude, config.elevation_m)
        agl = altitude_agl(beacon.altitude, terrain_m) if terrain_m is not None else agl_af
        # "At home" check: prefer polygon (precise) when configured, else
        # fall back to circular radius around the airfield centre.
        if config.home_polygon is not None:
            from shapely.geometry import Point
            at_home = config.home_polygon.contains(Point(beacon.lon, beacon.lat))
        else:
            at_home = dist_m <= config.home_radius_m

        now_mono = time.monotonic()
        now_iso = _utcnow_iso()
        ts = beacon.timestamp

        is_high = beacon.altitude > config.elevation_m + config.takeoff_alt_offset_m
        is_too_high = agl_af > config.takeoff_max_agl_m

        if flight is not None and flight._last_beacon_ts:
            # A beacon stamped in the future (receiver clock ahead) must not
            # become the reference, so cap it at wallclock.
            ref_ts = min(flight._last_beacon_ts, time.time())
            if ts < ref_ts - OUT_OF_ORDER_TOLERANCE_S:
                flight._ooo_drops += 1
                if flight._ooo_drops < OUT_OF_ORDER_MAX_DROPS:
                    log.debug(
                        "beacon_out_of_order_ignored",
                        flarm_id=beacon.flarm_id,
                        airfield=slug,
                        behind_s=int(ref_ts - ts),
                    )
                    return None
                log.info(
                    "beacon_timeline_reset",
                    flarm_id=beacon.flarm_id,
                    airfield=slug,
                    behind_s=int(ref_ts - ts),
                )
            flight._ooo_drops = 0

        if flight is None:
            # Not tracking this aircraft yet - check for takeoff
            if not at_home:
                return None  # Not at home airfield, discard

            # Ensure ground cache dict exists for this airfield
            if slug not in self._ground_cache:
                self._ground_cache[slug] = {}
            gc = self._ground_cache[slug]

            # Track aircraft seen stationary on the ground.
            # Use this single beacon's speed/AGL to decide ground contact;
            # the rolling buffer is only used for the takeoff trigger so a
            # single GPS speed glitch cannot fire takeoff prematurely.
            is_on_ground = (beacon.speed < config.ground_speed_max_kmh
                            and agl_af < config.ground_max_agl_m)
            if is_on_ground:
                entry = gc.get(beacon.flarm_id)
                if entry is None:
                    entry = {"first_seen": now_mono,
                             "speeds": deque(maxlen=SPEED_WINDOW_SIZE),
                             "fast_since_ts": 0.0,
                             "fast_count": 0}
                    gc[beacon.flarm_id] = entry
                    log.debug(
                        "ground_contact",
                        flarm_id=beacon.flarm_id,
                        airfield=slug,
                        speed=beacon.speed,
                        agl=round(agl_af),
                    )
                entry["speeds"].append(beacon.speed)
                entry["fast_since_ts"] = 0.0
                entry["fast_count"] = 0
                return None  # On ground, not airborne yet

            # Aircraft is moving/airborne - check for takeoff
            entry = gc.get(beacon.flarm_id)
            was_on_ground = entry is not None
            if was_on_ground:
                entry["speeds"].append(beacon.speed)
                avg_speed = sum(entry["speeds"]) / len(entry["speeds"])
            else:
                avg_speed = beacon.speed
            # Use rolling-average speed (smoothed over last N beacons) to
            # decide takeoff — robust against single-beacon GPS glitches.
            is_fast = avg_speed >= config.takeoff_speed_kmh

            confirmed = False
            if was_on_ground:
                # Remember when the ground roll started: first beacon at or
                # above takeoff speed (raw, so the smoothing lag does not
                # shift the takeoff time), reset if the roll is aborted.
                # fast_count = consecutive raw-fast beacons (confirmation).
                if beacon.speed >= config.takeoff_speed_kmh:
                    if not entry["fast_since_ts"]:
                        entry["fast_since_ts"] = ts
                    entry["fast_count"] += 1
                else:
                    entry["fast_since_ts"] = 0.0
                    entry["fast_count"] = 0
                confirmed = self._takeoff_confirmed(entry["fast_count"], config)

            if is_high and is_fast and not is_too_high and was_on_ground and not confirmed:
                # Looks like a lift-off, but only one fast beacon so far: a
                # single glitch must not start a flight. Wait for the next.
                log.debug(
                    "takeoff_unconfirmed",
                    flarm_id=beacon.flarm_id,
                    airfield=slug,
                    fast_beacons=entry["fast_count"],
                    speed=beacon.speed,
                    agl=round(agl_af),
                )
                return None

            if is_high and is_fast and not is_too_high and was_on_ground:
                # Aircraft was on ground and is now airborne - takeoff!
                gc.pop(beacon.flarm_id, None)
                roll_start = entry["fast_since_ts"]
                if roll_start and 0 <= ts - roll_start <= config.takeoff_roll_max_s:
                    takeoff_ts = roll_start
                else:
                    takeoff_ts = ts
                flight = FlightState(
                    flarm_id=beacon.flarm_id,
                    airfield_slug=slug,
                    airfield_id=config.id,
                    status=FlightStatus.TAKEOFF,
                    takeoff_time=_iso_from_ts(takeoff_ts),
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
                    takeoff_time=flight.takeoff_time,
                )
            elif not was_on_ground:
                log.debug(
                    "overflight_ignored",
                    flarm_id=beacon.flarm_id,
                    airfield=slug,
                    agl=round(agl_af),
                    speed=beacon.speed,
                    reason="not_seen_on_ground",
                )
                return None  # Never seen on ground - overflight
            else:
                return None  # At home but not yet taking off

        # ----- Landed flight: touch & go / final landing / restart -----
        # An already-landed flight stays in the hot state with status LANDING
        # so the tower controller still sees it. A re-takeoff of the SAME
        # aircraft is either a touch & go (same flight continues), a bounce
        # (landing retracted) or - once the landing is final - a new flight.
        elif flight.status == FlightStatus.LANDING:
            avg_speed = flight.push_speed(beacon.speed)
            is_fast = avg_speed >= config.takeoff_speed_kmh
            flight._last_beacon_ts = ts
            if not flight._landing_ts:
                flight._landing_ts = _ts_from_iso(flight.landing_time)
            ground_elapsed = ts - flight._landing_ts if flight._landing_ts else 1e9

            # Ground roll start for a possible restart (raw speed, see the
            # takeoff branch above for the rationale)
            if beacon.speed >= config.takeoff_speed_kmh:
                if not flight._restart_fast_since_ts:
                    flight._restart_fast_since_ts = ts
                flight._restart_fast_count += 1
            else:
                flight._restart_fast_since_ts = 0.0
                flight._restart_fast_count = 0
            restart_confirmed = self._takeoff_confirmed(
                flight._restart_fast_count, config
            )

            # "Airborne anywhere" -> terrain AGL (a go-around in a radio
            # hole may re-appear far from the runway)
            clearly_airborne = (agl > config.near_ground_band_m
                                and beacon.speed >= config.takeoff_speed_kmh)

            if (flight.landing_method == "silence" and not flight.landing_final
                    and clearly_airborne):
                # Aircraft re-appeared airborne (anywhere) before the silence
                # landing became final -> it was a radio hole on final /
                # go-around, not a landing (no phantom).
                self._retract_landing(slug, flight, "silence_phantom")
                # fall through: processed as an airborne beacon below
            elif (at_home and is_high and is_fast and not is_too_high
                    and not restart_confirmed):
                # Single fast + high beacon on a landed aircraft: glitch
                # guard, same as for the initial takeoff. Treated as a
                # ground beacon (position refresh) below.
                log.debug(
                    "restart_unconfirmed",
                    flarm_id=beacon.flarm_id,
                    airfield=slug,
                    fast_beacons=flight._restart_fast_count,
                    speed=beacon.speed,
                    agl=round(agl_af),
                )
                flight.last_seen = now_iso
                flight.elapsed_s = 0
                return flight
            elif at_home and is_high and is_fast and not is_too_high:
                if not flight.landing_final:
                    if ground_elapsed < config.bounce_debounce_s:
                        # Bounce / premature detection: not a landing at all
                        self._retract_landing(slug, flight, "bounce")
                    else:
                        self._touch_and_go(slug, flight, config, ground_elapsed)
                    # fall through: the beacon is processed as an airborne
                    # beacon of the continuing flight below
                else:
                    # Restart detected: archive the old flight and replace it.
                    old_flight = flight
                    self._emit_event(
                        slug, "flight_restarted", beacon.flarm_id, old_flight,
                        message="Flugzeug startet erneut",
                    )
                    log.info(
                        "flight_restarted",
                        flarm_id=beacon.flarm_id,
                        airfield=slug,
                        previous_landing=old_flight.landing_time,
                    )
                    roll_start = old_flight._restart_fast_since_ts
                    if roll_start and 0 <= ts - roll_start <= config.takeoff_roll_max_s:
                        takeoff_ts = roll_start
                    else:
                        takeoff_ts = ts
                    flight = FlightState(
                        flarm_id=beacon.flarm_id,
                        airfield_slug=slug,
                        airfield_id=config.id,
                        status=FlightStatus.TAKEOFF,
                        takeoff_time=_iso_from_ts(takeoff_ts),
                    )
                    self.flights[slug][beacon.flarm_id] = flight
                    self._emit_event(slug, "takeoff", beacon.flarm_id, flight)
            else:
                # Still on the ground - refresh last_seen and basic position
                flight.latitude = beacon.lat
                flight.longitude = beacon.lon
                flight.altitude_m = beacon.altitude
                flight.altitude_agl = agl
                flight.speed_kmh = beacon.speed
                flight.last_seen = now_iso
                flight.elapsed_s = 0
                # Touch & go confidence is about the runway -> airfield AGL
                flight._ground_min_agl = min(flight._ground_min_agl, agl_af)
                flight._ground_min_speed = min(flight._ground_min_speed, beacon.speed)
                if (not flight.landing_final
                        and ground_elapsed > self._final_delay_s(flight, config)):
                    self._finalize_landing(slug, flight)
                return flight

        # Push speed into the rolling buffer and use the smoothed value for
        # the landing decision (matches PyAcphFlightsLogbook's approach).
        avg_speed = flight.push_speed(beacon.speed)
        is_slow = avg_speed < config.landing_speed_kmh
        # Additional safeguard: only count as landed if also within an
        # altitude band around the airfield elevation. Prevents low+slow
        # turn-overhead manoeuvres from triggering a false landing.
        near_ground = abs(beacon.altitude - config.elevation_m) <= config.near_ground_band_m

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
        flight._last_beacon_ts = ts
        if beacon.device_type:
            flight.flarm_aircraft_type = beacon.device_type

        # Update extremes
        flight.max_altitude_m = max(flight.max_altitude_m, beacon.altitude)
        flight.max_distance_m = max(flight.max_distance_m, dist_m)

        old_status = flight.status

        # --- State transitions ---

        # Landing detection at home airfield
        # Requires: at home + smoothed speed below threshold + altitude band,
        # sustained for hysteresis_s of *beacon* time. The landing time is
        # the start of the slow phase (touchdown), not the end of the
        # hysteresis.
        if at_home and is_slow and near_ground and flight.status in AIRBORNE_STATUSES:
            if flight._slow_since == 0:
                flight._slow_since = ts
            elif (ts - flight._slow_since) >= config.hysteresis_s:
                self._land(
                    slug, flight, config,
                    landing_ts=flight._slow_since,
                    method="observed",
                    confidence=1.0,
                    message="Landung am Heimatplatz",
                )
                log.info(
                    "landing_detected",
                    flarm_id=beacon.flarm_id,
                    airfield=slug,
                    takeoff_time=flight.takeoff_time,
                    landing_time=flight.landing_time,
                    landing_count=flight.landing_count,
                )
        else:
            flight._slow_since = 0.0

        # Silence-landing candidate: remember the last beacon that looks
        # like a final approach at home (low, slow, not climbing). If the
        # aircraft then falls silent, check_timeouts() turns this into a
        # landing with landing_method='silence'. Any later beacon that does
        # not look like final resets the candidate (no phantom landings).
        if flight.status in AIRBORNE_STATUSES:
            if (at_home
                    and agl_af < config.near_ground_band_m
                    and beacon.vs <= SILENCE_MAX_VS_MS
                    and beacon.speed < config.landing_speed_kmh
                    + SILENCE_APPROACH_SPEED_MARGIN_KMH):
                conf = 0.6
                if beacon.speed < config.landing_speed_kmh + 10:
                    conf += 0.2
                if agl_af < config.near_ground_band_m / 2:
                    conf += 0.1
                if beacon.vs < 0:
                    conf += 0.1
                flight._silence_candidate_ts = ts
                flight._silence_candidate_conf = min(1.0, conf)
            else:
                flight._silence_candidate_ts = 0.0
                flight._silence_candidate_conf = 0.0

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

        # Outlanding detection: slow and low (terrain AGL), away from home
        if (flight.status in (FlightStatus.FLYING, FlightStatus.TAKEOFF)
                and not at_home and is_slow and agl < config.outlanding_max_agl_m):
            flight.status = FlightStatus.OUTLANDING_PENDING
            flight.outlanding_pending_since = now_mono

        # Outlanding recovery: speed picked up again
        if flight.status == FlightStatus.OUTLANDING_PENDING:
            if not is_slow or agl > config.outlanding_recover_agl_m:
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
        """Check all active flights for time-based transitions.

        Called periodically (every ~30s) by the worker. Handles:
        - landing_final for landed flights whose T&G window has passed
        - sticky-landed expiry
        - silence landings (final approach, then no beacons)
        - SIGNAL_LOST / ALARM escalation
        - OUTLANDING confirmation

        Returns:
            List of flights whose status changed or that need a Redis refresh.
        """
        changed = []
        now_mono = time.monotonic()

        for slug, af_flights in list(self.flights.items()):
            config = configs.get(slug)
            if not config:
                continue

            for flarm_id, flight in list(af_flights.items()):
                # Sticky landed: keep the entry visible for the configured
                # time AFTER LANDING — completely independent of incoming
                # beacons. Even if the FLARM is switched off the second the
                # plane has stopped, the entry stays put. Cleanup only when
                # the wallclock distance from landing_time exceeds the limit
                # OR when the same aircraft starts again (handled in
                # process_beacon).
                if flight.status == FlightStatus.LANDING:
                    age_s = _seconds_since_iso(flight.landing_time)
                    quiet_s = _seconds_since_iso(flight.last_seen)
                    if (not flight.landing_final
                            and age_s is not None
                            and age_s > self._final_delay_s(flight, config)
                            and quiet_s is not None
                            and quiet_s > self._final_delay_s(flight, config)):
                        # FLARM went quiet after landing: no more beacons
                        # will finalize it, so do it here. While beacons
                        # keep coming, the beacon path decides on beacon
                        # time (a delayed feed must not turn a touch & go
                        # into a restart).
                        self._finalize_landing(slug, flight)
                    if age_s is not None and age_s > config.sticky_landed_max_age_s:
                        self._emit_event(
                            slug, "sticky_landed_expired", flarm_id, flight,
                            message="Anzeigedauer fuer gelandeten Flug erreicht",
                        )
                        log.info(
                            "sticky_landed_expired",
                            flarm_id=flarm_id,
                            airfield=slug,
                            age_s=int(age_s),
                        )
                        changed.append(flight)
                    else:
                        # Tag as "still pinned" so the tracker re-writes it
                        # to Redis and refreshes the TTL even though the
                        # FLARM is silent.
                        changed.append(flight)
                    continue

                # Seconds since the last beacon (wallclock). Falls back to
                # the old "+30 per tick" approximation if last_seen is unset.
                age_s = _seconds_since_iso(flight.last_seen)
                if age_s is None:
                    flight.elapsed_s += 30
                else:
                    flight.elapsed_s = int(age_s)

                # Silence landing: the last beacons looked like a final
                # approach at home and the aircraft has been quiet since.
                # Checked before SIGNAL_LOST so a switched-off FLARM on the
                # apron does not escalate into an alarm.
                if (flight._silence_candidate_ts
                        and flight.status in AIRBORNE_STATUSES
                        and flight.elapsed_s > config.silence_landing_s):
                    self._land(
                        slug, flight, config,
                        landing_ts=flight._silence_candidate_ts,
                        method="silence",
                        confidence=flight._silence_candidate_conf,
                        message="Landung am Heimatplatz (Funkstille nach Endanflug)",
                    )
                    log.info(
                        "silence_landing_detected",
                        flarm_id=flarm_id,
                        airfield=slug,
                        landing_time=flight.landing_time,
                        confidence=flight.landing_confidence,
                    )
                    changed.append(flight)
                    continue

                # Stage 1: SIGNAL_LOST (yellow). Triggered when a flying
                # aircraft hasn't been heard from for signal_loss_timeout_s.
                if (flight.elapsed_s > config.signal_loss_timeout_s
                        and flight.status in (FlightStatus.FLYING,
                                              FlightStatus.TAKEOFF,
                                              FlightStatus.TOWING)):
                    flight.status = FlightStatus.SIGNAL_LOST
                    self._emit_event(
                        slug, "signal_lost", flarm_id, flight,
                        message=(
                            f"Kein Signal seit {flight.elapsed_s // 60} Min. "
                            f"Letzte Position: {flight.distance_m/1000:.1f}km "
                            f"{flight.bearing_text}, {flight.altitude_m:.0f}m"
                        ),
                    )
                    log.info(
                        "signal_lost",
                        flarm_id=flarm_id,
                        airfield=slug,
                        elapsed_s=flight.elapsed_s,
                    )
                    changed.append(flight)

                # Stage 2: ALARM (red). Escalation from SIGNAL_LOST after
                # alarm_timeout_s — this is the "really missing" state.
                if (flight.elapsed_s > config.alarm_timeout_s
                        and flight.status == FlightStatus.SIGNAL_LOST):
                    flight.status = FlightStatus.ALARM
                    self._emit_event(
                        slug, "alarm", flarm_id, flight,
                        message=(
                            f"VERMISST seit {flight.elapsed_s // 60} Min. "
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
            stale = [fid for fid, e in gc.items()
                     if (now_mono - e["first_seen"]) > self.GROUND_CACHE_MAX_AGE_S]
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

    def discard_ground_contact(self, airfield_slug: str, flarm_id: str) -> None:
        """Forget a ground-cache entry (aircraft seen stationary at home).

        Used when a device must no longer be tracked at all (DDB opt-out).
        """
        gc = self._ground_cache.get(airfield_slug)
        if gc:
            gc.pop(flarm_id, None)

    def restore_flight(self, airfield_slug: str, flight: FlightState) -> None:
        """Restore a flight from Redis (on worker restart)."""
        if airfield_slug not in self.flights:
            self.flights[airfield_slug] = {}
        if flight.status == FlightStatus.LANDING and flight.landing_time:
            flight._landing_ts = _ts_from_iso(flight.landing_time)
        self.flights[airfield_slug][flight.flarm_id] = flight

    # ------------------------------------------------------------------
    # Landing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _takeoff_confirmed(fast_count: int, config: AirfieldConfig) -> bool:
        """Whether enough consecutive raw-fast beacons were seen to declare
        a takeoff / restart / touch & go (glitch guard).

        ``takeoff_min_fast_beacons <= 1`` restores the legacy behaviour:
        declare on the first beacon that is high and (smoothed) fast.
        """
        if config.takeoff_min_fast_beacons <= 1:
            return True
        return fast_count >= config.takeoff_min_fast_beacons

    @staticmethod
    def _final_delay_s(flight: FlightState, config: AirfieldConfig) -> int:
        """Seconds after landing_time until the landing counts as final.

        Observed landings: the touch & go window. Silence landings are
        declared silence_landing_s after the last beacon (landing_time is
        that last beacon), so add that offset: the effective grace for a
        late re-appearance (go-around in a radio hole) is again the touch
        & go window, counted from the declaration.
        """
        delay = config.touch_go_max_ground_s
        if flight.landing_method == "silence":
            delay += config.silence_landing_s
        return delay

    def _land(self, slug: str, flight: FlightState, config: AirfieldConfig,
              landing_ts: float, method: str, confidence: float,
              message: str) -> None:
        """Transition an airborne flight to LANDING and emit the event."""
        flight.status = FlightStatus.LANDING
        flight.landing_time = _iso_from_ts(landing_ts)
        flight._landing_ts = landing_ts
        flight.landing_method = method
        flight.landing_confidence = round(confidence, 2)
        flight.landing_final = False
        flight._slow_since = 0.0
        flight._silence_candidate_ts = 0.0
        flight._silence_candidate_conf = 0.0
        # Runway-relative (touch & go confidence), not terrain AGL
        flight._ground_min_agl = altitude_agl(flight.altitude_m, config.elevation_m)
        flight._ground_min_speed = flight.speed_kmh
        self._emit_event(slug, "landing", flight.flarm_id, flight, message=message)

    def _finalize_landing(self, slug: str, flight: FlightState) -> None:
        """Mark a landing as final (no touch & go possible any more)."""
        flight.landing_final = True
        self._emit_event(
            slug, "landing_final", flight.flarm_id, flight,
            message=f"Landung bestaetigt ({flight.landing_count} Landung(en))",
        )
        log.info(
            "landing_final",
            flarm_id=flight.flarm_id,
            airfield=slug,
            landing_time=flight.landing_time,
            landing_count=flight.landing_count,
            landing_method=flight.landing_method,
        )

    def _retract_landing(self, slug: str, flight: FlightState, reason: str) -> None:
        """Undo a landing that turned out not to be one (bounce / phantom)."""
        log.info(
            "landing_retracted",
            flarm_id=flight.flarm_id,
            airfield=slug,
            reason=reason,
            landing_time=flight.landing_time,
        )
        flight.status = FlightStatus.FLYING
        flight.landing_time = ""
        flight._landing_ts = 0.0
        flight.landing_method = ""
        flight.landing_confidence = 0.0
        flight.landing_final = False
        flight._restart_fast_since_ts = 0.0
        flight._restart_fast_count = 0
        flight.reset_speed_window()
        self._emit_event(
            slug, "landing_retracted", flight.flarm_id, flight,
            message="Landung zurueckgenommen (Durchstart)",
        )

    def _touch_and_go(self, slug: str, flight: FlightState, config: AirfieldConfig,
                      ground_elapsed: float) -> None:
        """Continue the same flight after a touch & go (landing_count++)."""
        conf = 0.5
        if flight._ground_min_agl < TG_CONF_MIN_AGL_M:
            conf += 0.2
        if flight._ground_min_speed < config.landing_speed_kmh - TG_CONF_SPEED_BELOW_LANDING:
            conf += 0.2
        if ground_elapsed >= TG_CONF_MIN_GROUND_S:
            conf += 0.1
        conf = min(1.0, conf)

        flight.landing_count += 1
        flight.touch_go_confidence = round(conf, 2)
        flight.status = FlightStatus.FLYING
        flight.landing_time = ""
        flight._landing_ts = 0.0
        flight.landing_method = ""
        flight.landing_confidence = 0.0
        flight.landing_final = False
        flight._restart_fast_since_ts = 0.0
        flight._restart_fast_count = 0
        flight.reset_speed_window()
        log.info(
            "touch_and_go",
            flarm_id=flight.flarm_id,
            airfield=slug,
            landing_count=flight.landing_count,
            ground_s=int(ground_elapsed),
            confidence=conf,
        )
        self._emit_event(
            slug, "touch_and_go", flight.flarm_id, flight,
            message=f"Touch & Go ({flight.landing_count}. Landung, {int(ground_elapsed)}s am Boden)",
        )

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
