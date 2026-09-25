"""Flight Tracker - Central orchestration of the 2-stage beacon pipeline.

Coordinates all tracking components:
- Beacon parsing -> Flight State Machine -> Redis Hot State
- Launch detection (winch/aerotow/self)
- Profile buffer for signal loss analysis
- Aircraft resolution (FLARM ID -> registration)
- Event publishing via Redis PubSub

This is the main entry point called by the APRS Worker for each beacon.

OGN DDB privacy: this class is the single choke point for the DDB
``tracked = N`` opt-out. Beacons of such devices are dropped here, before
any state machine, hot-state, track-stream, event or flight_log write
(see docs/dev-guides/implement-flight-logic.md).

Terrain AGL: for beacons of an already tracked flight the ground elevation
under the aircraft is resolved from ``ElevationService`` (cache, DB on a
miss) *before* the CPU-bound state machine call and passed in. Beacons of
untracked aircraft (thousands per minute inside the APRS filter radius)
never trigger a lookup: a takeoff can only happen at the home airfield,
where the airfield elevation is the right reference anyway.
"""

import asyncio
import time

import structlog

from app.aprs.beacon_parser import Beacon, parse_beacon
from app.config import settings
from app.data.aircraft_resolver import AircraftResolver
from app.tracking.elevation import ElevationService
from app.tracking.flight_profile_analyzer import FlightEndClassification, analyze_profile
from app.tracking.flight_profile_buffer import FlightProfileBuffer
from app.tracking.flight_state import FlightState, FlightStatus
from app.tracking.flight_state_machine import AirfieldConfig, FlightStateMachine
from app.tracking.launch_detector import LaunchDetector
from app.tracking.redis_writer import SIMULATED_FIELD, SIMULATED_VALUE, RedisWriter

log = structlog.get_logger()

# Dropped beacons of an untracked device are logged (debug) at most once
# per device within this interval - a switched-on FLARM sends ~1 beacon/s.
UNTRACKED_LOG_INTERVAL_S = 3600

# flight_status rows of evicted (tracked = N) aircraft whose DELETE failed
# are retried in check_timeouts(); the retry set is bounded, best effort.
STATUS_DELETE_RETRY_MAX = 64


class FlightTracker:
    """Central flight tracking orchestrator.

    Receives raw APRS lines, parses them, runs them through the state machine,
    and writes results to Redis.
    """

    def __init__(
        self,
        redis_writer: RedisWriter,
        aircraft_resolver: AircraftResolver,
        elevation: ElevationService | None = None,
    ):
        self.redis_writer = redis_writer
        self.aircraft_resolver = aircraft_resolver
        # Terrain model lookup; None = airfield elevation everywhere
        self.elevation = elevation
        self.state_machine = FlightStateMachine()
        self.launch_detector = LaunchDetector()
        self.profile_buffer = FlightProfileBuffer()
        # Lazy import to avoid hard coupling at module load time
        from app.tracking.state_synchronizer import StateSynchronizer
        self._state_sync = StateSynchronizer()

        # Airfield configs: slug -> AirfieldConfig
        self._configs: dict[str, AirfieldConfig] = {}

        # Beacon time (epoch s) of the last point written to the
        # per-aircraft track stream, keyed by "{slug}:{flarm_id}" (thinning).
        self._last_track_ts: dict[str, float] = {}

        # DDB tracked=N: dropped-beacon counter and last debug-log time
        # (monotonic) per flarm_id, for the rate-limited log only.
        self._untracked_drops: dict[str, int] = {}
        self._untracked_logged_at: dict[str, float] = {}
        # (airfield_id, flarm_id) of evicted flights whose flight_status
        # DELETE failed; retried in check_timeouts().
        self._status_delete_retry: set[tuple[int, str]] = set()

    def set_configs(self, configs: dict[str, AirfieldConfig]) -> None:
        """Update airfield configurations."""
        self._configs = configs
        log.info("tracker_configs_updated", airfield_count=len(configs))

    async def process_line(self, line: str) -> None:
        """Process a raw APRS-IS line through the full pipeline.

        This is the main entry point called by the APRS client callback.
        """
        # Stage 1: Parse beacon
        beacon = parse_beacon(line)
        if not beacon:
            return

        # DDB privacy: opted-out devices never reach any airfield
        if self._is_untracked(beacon.flarm_id):
            await self._drop_untracked_beacon(beacon)
            return

        # Stage 2: Try each airfield config
        for slug, config in self._configs.items():
            tracked_here = self.state_machine.get_flight(slug, beacon.flarm_id) is not None
            flight = await self._process_beacon_for_airfield(beacon, config)
            if flight or tracked_here:
                # A flight belongs to at most one airfield - also when its
                # beacon was dropped (out of order), never offer it to the
                # next airfield.
                break

    async def _process_beacon_for_airfield(
        self, beacon: Beacon, config: AirfieldConfig
    ) -> FlightState | None:
        """Process a beacon for a specific airfield.

        Returns the updated FlightState or None if discarded.
        """
        slug = config.slug

        # DDB privacy choke point (also for direct callers / tests):
        # nothing below may run for an opted-out device.
        if self._is_untracked(beacon.flarm_id):
            await self._drop_untracked_beacon(beacon)
            return None

        # Check if we're already tracking this flight at this airfield
        existing = self.state_machine.get_flight(slug, beacon.flarm_id)
        old_status = existing.status if existing else None

        # Terrain under the aircraft (async, before the CPU-bound call);
        # only for tracked flights, see the module docstring.
        terrain_m = None
        if existing is not None:
            terrain_m = await self._terrain_elevation(beacon.lat, beacon.lon)

        # Run through state machine
        flight = self.state_machine.process_beacon(beacon, config, terrain_m=terrain_m)
        if not flight:
            return None

        # Enrich with aircraft info
        if not flight.registration:
            info = self.aircraft_resolver.resolve(beacon.flarm_id)
            if info:
                flight.registration = info.registration
                flight.aircraft_model = info.aircraft_model
                flight.competition_sign = info.competition_sign
                flight.aircraft_role = info.role
            elif beacon.registration:
                self.aircraft_resolver.update_from_aprs(
                    beacon.flarm_id, beacon.registration
                )
                flight.registration = beacon.registration

        # Update profile buffer
        self.profile_buffer.add(
            beacon.flarm_id,
            beacon.timestamp,
            beacon.lat, beacon.lon, beacon.altitude,
            beacon.speed, beacon.vs, beacon.track,
        )

        # Launch detection (per airfield: pairing only among flights of
        # this field). Every beacon is offered to the detector because a
        # resolved tow plane still feeds the towplane_max fallback of the
        # glider it is towing.
        new_status = flight.status
        airfield_flights = list(self.state_machine.get_all_flights(slug).values())
        is_new_takeoff = (
            new_status == FlightStatus.TAKEOFF
            and (old_status is None or old_status == FlightStatus.LANDING)
        )
        if is_new_takeoff:
            if old_status == FlightStatus.LANDING:
                # Restart: drop the detection state of the archived flight
                self.launch_detector.cleanup(beacon.flarm_id)
            self.launch_detector.on_takeoff(flight, airfield_flights, config)
        self.launch_detector.on_beacon(flight, beacon, airfield_flights, config)

        # Write to Redis. Landed flights get an extended TTL so that the
        # entry survives even if the FLARM is switched off right after
        # landing — visibility is bounded by the wallclock-based sticky
        # cleanup, not by Redis key expiry.
        redis_data = flight.to_redis_dict()
        ttl = None
        if flight.status == FlightStatus.LANDING:
            ttl = config.sticky_landed_max_age_s + 3600
        await self.redis_writer.update_flight(
            slug, beacon.flarm_id, redis_data, ttl=ttl
        )

        # Publish beacon update
        await self.redis_writer.publish_beacon(slug, beacon.flarm_id, {
            "latitude": round(beacon.lat, 5),
            "longitude": round(beacon.lon, 5),
            "altitude_m": round(beacon.altitude),
            "altitude_agl": round(flight.altitude_agl),
            "speed_kmh": round(beacon.speed),
            "vertical_speed_ms": round(beacon.vs, 1),
            "track_deg": round(beacon.track),
            "status": flight.status.value,
            "qdr_deg": round(flight.qdr_deg),
            "distance_m": round(flight.distance_m),
            "bearing_text": flight.bearing_text,
            "last_seen": flight.last_seen,
        })

        # Add to position stream
        await self.redis_writer.add_position(
            slug, beacon.flarm_id,
            beacon.lat, beacon.lon, beacon.altitude,
            beacon.speed, beacon.vs, beacon.track,
        )

        # Thinned per-aircraft track stream (monitor map, last 24h)
        track_key = f"{slug}:{beacon.flarm_id}"
        last_track = self._last_track_ts.get(track_key)
        if (last_track is None
                or beacon.timestamp - last_track >= settings.track_min_interval_s):
            self._last_track_ts[track_key] = beacon.timestamp
            await self.redis_writer.add_track_point(
                slug, beacon.flarm_id, int(beacon.timestamp * 1000),
                beacon.lat, beacon.lon, beacon.altitude, flight.altitude_agl,
                beacon.speed, beacon.vs, beacon.track,
                retention_s=settings.track_retention_s,
                min_interval_s=settings.track_min_interval_s,
            )

        # Process and publish state machine events (incl. archival on
        # restart / sticky-landed-expired).
        await self._dispatch_events()

        return flight

    async def _terrain_elevation(self, lat: float, lon: float) -> float | None:
        """Ground elevation under a position, or None (fallback: airfield)."""
        if self.elevation is None or not settings.terrain_agl_enabled:
            return None
        return await self.elevation.get(lat, lon)

    async def classify_flight_end(
        self, flight: FlightState, config: AirfieldConfig
    ) -> FlightEndClassification:
        """Classify a signal loss with the profile buffer (flight_profile_analyzer).

        The analyzer's ``ground_elevation_m`` is the ground under the
        *last known position*: the terrain model when available, the
        airfield elevation otherwise. Not called by the beacon path yet
        (the analyzer has no production consumer); ready for the alarm
        escalation.
        """
        ground = None
        if flight.latitude or flight.longitude:
            ground = await self._terrain_elevation(flight.latitude, flight.longitude)
        if ground is None:
            ground = config.elevation_m
        return analyze_profile(
            self.profile_buffer.get(flight.flarm_id),
            ground_elevation_m=ground,
        )

    async def check_timeouts(self) -> None:
        """Check all flights for signal loss timeouts.

        Called periodically (~30s) by the worker.
        """
        # DDB reload may have flipped tracked -> N for a flight in progress
        # whose FLARM is already off (no beacon will ever evict it).
        await self._evict_untracked_flights()
        await self._retry_flight_status_deletes()

        changed = self.state_machine.check_timeouts(self._configs)

        for flight in changed:
            # Sticky-landed flights need a TTL that comfortably covers
            # the configured visibility window — even if the FLARM is off
            # and no beacons refresh the key. Other flights use the default.
            ttl = None
            if flight.status == FlightStatus.LANDING:
                cfg = self._configs.get(flight.airfield_slug)
                if cfg is not None:
                    # Add a 1h cushion so the cleanup event always wins
                    # over the raw key expiry.
                    ttl = cfg.sticky_landed_max_age_s + 3600
            redis_data = flight.to_redis_dict()
            await self.redis_writer.update_flight(
                flight.airfield_slug, flight.flarm_id, redis_data, ttl=ttl
            )

        # Publish any events generated by timeout checks (this also
        # triggers sticky-landed cleanup if a 24h flight expired).
        await self._dispatch_events()

    async def _dispatch_events(self) -> None:
        """Drain pending state-machine and launch-detector events, publish
        them, and react to lifecycle events that need follow-up actions
        (archive, cleanup).
        """
        events = self.state_machine.drain_events() + self.launch_detector.drain_events()
        for event in events:
            etype = event["event_type"]
            slug = event["airfield_slug"]
            fid = event["flarm_id"]
            old_flight: FlightState = event["flight"]

            await self.redis_writer.publish_event(
                slug, etype, fid,
                data=old_flight.to_redis_dict(),
                message=event.get("message", ""),
            )

            # Launch type may be decided on ANOTHER aircraft's beacon
            # (tow-plane side fallback, partner settlement, stale
            # closure): refresh that flight's hash so API/reload see it.
            if etype == "launch_type_detected":
                ttl = None
                if old_flight.status == FlightStatus.LANDING:
                    cfg = self._configs.get(slug)
                    if cfg is not None:
                        ttl = cfg.sticky_landed_max_age_s + 3600
                await self.redis_writer.update_flight(
                    slug, fid, old_flight.to_redis_dict(), ttl=ttl
                )

            # Final landing (touch & go window passed): persist to
            # flight_log right away so downstream consumers (VF-Sync
            # recovery, flight log page) see it the same day. The upsert
            # is idempotent, a later archive only refreshes it.
            elif etype == "landing_final":
                await self._archive_to_log(old_flight)
                self.profile_buffer.remove(old_flight.flarm_id)

            # Restart: archive the old flight to flight_log; the new
            # FlightState already replaced it in the state machine and its
            # launch detection was (re)started in _process_beacon_for_airfield
            # - do NOT clean up the detector here, that would drop the new
            # flight's state (same FLARM id).
            elif etype == "flight_restarted":
                await self._archive_to_log(old_flight)

            # Sticky landed cleanup after 24h: archive AND remove from
            # the hot state.
            elif etype == "sticky_landed_expired":
                await self._archive_to_log(old_flight)
                await self._archive_flight(slug, old_flight)

    async def recover_from_redis(self) -> int:
        """Recover active flights from Redis on worker restart.

        Simulated flights (``app.vfsync.simulate``, hash field
        ``simulated=1``) are skipped and left in Redis: they stay visible
        in the monitor until their TTL expires but must never enter the
        state machine (and thus flight_status / flight_log).

        Returns number of restored flights.
        """
        count = 0
        for slug in self._configs:
            flarm_ids = await self.redis_writer.get_active_flights(slug)
            for fid in flarm_ids:
                data = await self.redis_writer.get_flight(slug, fid)
                if data:
                    if data.get(SIMULATED_FIELD) == SIMULATED_VALUE:
                        log.info("flight_recovery_skipped_simulated", slug=slug, flarm_id=fid)
                        continue
                    if self._is_untracked(fid):
                        # DDB opt-out while the worker was down: purge
                        # instead of restoring (no flight_log either).
                        await self.redis_writer.remove_flight(slug, fid)
                        await self.redis_writer.delete_track(slug, fid)
                        log.info("flight_recovery_dropped_untracked", slug=slug, flarm_id=fid)
                        continue
                    flight = FlightState.from_redis(data, slug)
                    if flight.flarm_id:
                        config = self._configs.get(slug)
                        if config:
                            flight.airfield_id = config.id
                        self.state_machine.restore_flight(slug, flight)
                        count += 1

        if count:
            log.info("flights_recovered_from_redis", count=count)
        return count

    async def _archive_flight(self, airfield_slug: str, flight: FlightState) -> None:
        """Remove a flight entirely from the hot state."""
        self.state_machine.archive_flight(airfield_slug, flight.flarm_id)
        self.launch_detector.cleanup(flight.flarm_id)
        self.profile_buffer.remove(flight.flarm_id)
        self._last_track_ts.pop(f"{airfield_slug}:{flight.flarm_id}", None)
        await self.redis_writer.remove_flight(airfield_slug, flight.flarm_id)

        log.info(
            "flight_archived",
            flarm_id=flight.flarm_id,
            registration=flight.registration,
            airfield=airfield_slug,
            launch_type=flight.launch_type,
        )

    # ------------------------------------------------------------------
    # OGN DDB privacy: tracked = N
    # ------------------------------------------------------------------

    def _is_untracked(self, flarm_id: str) -> bool:
        """True if the DDB says the device owner opted out of tracking."""
        info = self.aircraft_resolver.resolve(flarm_id)
        return info is not None and not info.tracked

    async def _drop_untracked_beacon(self, beacon: Beacon) -> None:
        """Discard a beacon of an opted-out device.

        Evicts the aircraft from every airfield where it is still tracked
        (the flag may have flipped on a DDB reload while it was flying),
        counts the drop and logs at debug level at most once per device
        and UNTRACKED_LOG_INTERVAL_S.
        """
        fid = beacon.flarm_id
        for slug in list(self._configs):
            flight = self.state_machine.get_flight(slug, fid)
            if flight is not None:
                await self._evict_untracked_flight(slug, flight)
            self.state_machine.discard_ground_contact(slug, fid)

        count = self._untracked_drops.get(fid, 0) + 1
        self._untracked_drops[fid] = count
        now = time.monotonic()
        last = self._untracked_logged_at.get(fid)
        if last is None or now - last >= UNTRACKED_LOG_INTERVAL_S:
            self._untracked_logged_at[fid] = now
            log.debug("beacon_dropped_untracked", flarm_id=fid, dropped=count)

    async def _evict_untracked_flights(self) -> None:
        """Evict every active flight whose device is (now) untracked."""
        for flight in list(self.state_machine.get_all_active_flights()):
            if self._is_untracked(flight.flarm_id):
                await self._evict_untracked_flight(flight.airfield_slug, flight)

    async def _evict_untracked_flight(self, slug: str, flight: FlightState) -> None:
        """Remove a flight of an opted-out device from every store.

        Unlike a normal archive this also drops the 24 h track stream and
        the flight_status row, and it never writes flight_log. Events the
        eviction might leave behind are discarded, not published; flights
        paired with the evicted aircraft (tow partner) lose every
        reference to it before anything is written.
        """
        fid = flight.flarm_id
        await self._blank_partner_refs(slug, fid)
        await self._archive_flight(slug, flight)
        await self.redis_writer.delete_track(slug, fid)
        await self._delete_flight_status(flight)
        # Nothing about this aircraft may reach subscribers; events of
        # other aircraft (e.g. a tow partner) are kept and go out now,
        # not on the next beacon.
        pending = self.state_machine.drain_events() + self.launch_detector.drain_events()
        self.state_machine.requeue_events(
            [e for e in pending if e["flarm_id"] != fid]
        )
        log.info(
            "flight_evicted_untracked",
            flarm_id=fid,
            airfield=slug,
            status=flight.status.name,
        )
        await self._dispatch_events()

    async def _blank_partner_refs(self, slug: str, flarm_id: str) -> None:
        """Remove an evicted aircraft from the flights it was paired with.

        Pending detections forget the pairing (launch detector); flights
        already resolved with it as tow plane get ``tow_plane_flarm_id`` /
        ``tow_plane_reg`` blanked and their hot-state hash rewritten so
        that no later event, sync or archive carries the reference.
        """
        self.launch_detector.forget_partner(slug, flarm_id)
        for other in self.state_machine.get_all_flights(slug).values():
            if other.flarm_id == flarm_id or other.tow_plane_flarm_id != flarm_id:
                continue
            other.tow_plane_flarm_id = ""
            other.tow_plane_reg = ""
            ttl = None
            if other.status == FlightStatus.LANDING:
                cfg = self._configs.get(slug)
                if cfg is not None:
                    ttl = cfg.sticky_landed_max_age_s + 3600
            await self.redis_writer.update_flight(
                slug, other.flarm_id, other.to_redis_dict(), ttl=ttl
            )
            log.info(
                "flight_partner_reference_blanked",
                flarm_id=other.flarm_id,
                partner=flarm_id,
                airfield=slug,
            )

    async def _delete_flight_status(self, flight: FlightState) -> None:
        """Delete the cold-store flight_status row of an evicted flight.

        A failed DELETE is remembered (bounded) and retried in
        ``check_timeouts()``.
        """
        key = (flight.airfield_id, flight.flarm_id)
        if await self._try_delete_flight_status(*key):
            return
        if len(self._status_delete_retry) < STATUS_DELETE_RETRY_MAX:
            self._status_delete_retry.add(key)
        else:
            log.warning(
                "flight_status_delete_retry_dropped",
                flarm_id=flight.flarm_id,
                pending=len(self._status_delete_retry),
            )

    async def _try_delete_flight_status(self, airfield_id: int, flarm_id: str) -> bool:
        """One DELETE attempt; True on success, False (logged) on error."""
        try:
            from app.db.connection import get_db
            db = get_db()
            await self._state_sync.delete_flight_status(db, airfield_id, flarm_id)
            return True
        except Exception:
            log.exception("flight_status_delete_failed", flarm_id=flarm_id)
            return False

    async def _retry_flight_status_deletes(self) -> None:
        """Retry the flight_status DELETEs that failed earlier (best effort)."""
        for key in list(self._status_delete_retry):
            if await self._try_delete_flight_status(*key):
                self._status_delete_retry.discard(key)

    async def _archive_to_log(self, flight: FlightState) -> None:
        """Persist a completed flight to the flight_log table."""
        try:
            from app.db.connection import get_db
            db = get_db()
            await self._state_sync._write_flight_log(db, flight, flight.status)
        except Exception:
            log.exception(
                "flight_log_write_failed",
                flarm_id=flight.flarm_id,
            )
