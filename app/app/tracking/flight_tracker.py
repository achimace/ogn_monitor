"""Flight Tracker - Central orchestration of the 2-stage beacon pipeline.

Coordinates all tracking components:
- Beacon parsing -> Flight State Machine -> Redis Hot State
- Launch detection (winch/aerotow/self)
- Profile buffer for signal loss analysis
- Aircraft resolution (FLARM ID -> registration)
- Event publishing via Redis PubSub

This is the main entry point called by the APRS Worker for each beacon.
"""

import asyncio

import structlog

from app.aprs.beacon_parser import Beacon, parse_beacon
from app.data.aircraft_resolver import AircraftResolver
from app.tracking.flight_profile_buffer import FlightProfileBuffer
from app.tracking.flight_state import FlightState, FlightStatus
from app.tracking.flight_state_machine import AirfieldConfig, FlightStateMachine
from app.tracking.launch_detector import LaunchDetector
from app.tracking.redis_writer import RedisWriter

log = structlog.get_logger()


class FlightTracker:
    """Central flight tracking orchestrator.

    Receives raw APRS lines, parses them, runs them through the state machine,
    and writes results to Redis.
    """

    def __init__(
        self,
        redis_writer: RedisWriter,
        aircraft_resolver: AircraftResolver,
    ):
        self.redis_writer = redis_writer
        self.aircraft_resolver = aircraft_resolver
        self.state_machine = FlightStateMachine()
        self.launch_detector = LaunchDetector()
        self.profile_buffer = FlightProfileBuffer()

        # Airfield configs: slug -> AirfieldConfig
        self._configs: dict[str, AirfieldConfig] = {}

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

        # Stage 2: Try each airfield config
        for slug, config in self._configs.items():
            flight = await self._process_beacon_for_airfield(beacon, config)
            if flight:
                break  # A flight belongs to at most one airfield

    async def _process_beacon_for_airfield(
        self, beacon: Beacon, config: AirfieldConfig
    ) -> FlightState | None:
        """Process a beacon for a specific airfield.

        Returns the updated FlightState or None if discarded.
        """
        slug = config.slug

        # Check if we're already tracking this flight at this airfield
        existing = self.state_machine.get_flight(slug, beacon.flarm_id)
        old_status = existing.status if existing else None

        # Run through state machine
        flight = self.state_machine.process_beacon(beacon, config)
        if not flight:
            return None

        # Enrich with aircraft info
        if not flight.registration:
            info = self.aircraft_resolver.resolve(beacon.flarm_id)
            if info:
                flight.registration = info.registration
                flight.aircraft_model = info.aircraft_model
                flight.competition_sign = info.competition_sign
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

        # Launch detection
        new_status = flight.status
        if old_status is None and new_status == FlightStatus.TAKEOFF:
            # New takeoff
            all_flights = self.state_machine.get_all_active_flights()
            self.launch_detector.on_takeoff(flight, all_flights)
        elif self.launch_detector.is_pending(beacon.flarm_id):
            all_flights = self.state_machine.get_all_active_flights()
            self.launch_detector.on_beacon(flight, beacon, all_flights)

        # Write to Redis
        redis_data = flight.to_redis_dict()
        await self.redis_writer.update_flight(slug, beacon.flarm_id, redis_data)

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

        # Process and publish state machine events
        for event in self.state_machine.drain_events():
            await self.redis_writer.publish_event(
                event["airfield_slug"],
                event["event_type"],
                event["flarm_id"],
                data=event["flight"].to_redis_dict(),
                message=event.get("message", ""),
            )

        # Handle landing: archive and cleanup
        if flight.status == FlightStatus.LANDING:
            await self._archive_flight(slug, flight)

        return flight

    async def check_timeouts(self) -> None:
        """Check all flights for signal loss timeouts.

        Called periodically (~30s) by the worker.
        """
        changed = self.state_machine.check_timeouts(self._configs)

        for flight in changed:
            # Write updated state to Redis
            redis_data = flight.to_redis_dict()
            await self.redis_writer.update_flight(
                flight.airfield_slug, flight.flarm_id, redis_data
            )

        # Publish any events generated by timeout checks
        for event in self.state_machine.drain_events():
            await self.redis_writer.publish_event(
                event["airfield_slug"],
                event["event_type"],
                event["flarm_id"],
                data=event["flight"].to_redis_dict(),
                message=event.get("message", ""),
            )

    async def recover_from_redis(self) -> int:
        """Recover active flights from Redis on worker restart.

        Returns number of restored flights.
        """
        count = 0
        for slug in self._configs:
            flarm_ids = await self.redis_writer.get_active_flights(slug)
            for fid in flarm_ids:
                data = await self.redis_writer.get_flight(slug, fid)
                if data:
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
        """Archive a landed flight: remove from active tracking."""
        self.state_machine.archive_flight(airfield_slug, flight.flarm_id)
        self.launch_detector.cleanup(flight.flarm_id)
        self.profile_buffer.remove(flight.flarm_id)
        await self.redis_writer.remove_flight(airfield_slug, flight.flarm_id)

        log.info(
            "flight_archived",
            flarm_id=flight.flarm_id,
            registration=flight.registration,
            airfield=airfield_slug,
            launch_type=flight.launch_type,
        )
