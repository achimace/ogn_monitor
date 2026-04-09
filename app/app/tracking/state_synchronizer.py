"""State Synchronizer - Redis Hot State to PostgreSQL Cold Storage.

Handles:
- Periodic bulk sync (every 30s) for position updates
- Immediate writes on status changes (takeoff, landing, alarm, etc.)
- Flight log archival on landing/outlanding

This reduces PostgreSQL writes from ~1000/min to ~5/min.
"""

import asyncio
from datetime import datetime, timezone

import structlog

from app.config import settings
from app.db.connection import get_db
from app.tracking.flight_state import FlightState, FlightStatus, EVENT_STATUSES

log = structlog.get_logger()


class StateSynchronizer:
    """Synchronizes flight state from Redis/memory to PostgreSQL."""

    def __init__(self):
        self._sync_interval = settings.redis_sync_interval

    async def on_status_change(self, flight: FlightState, old_status: FlightStatus,
                               new_status: FlightStatus) -> None:
        """Immediately write to PostgreSQL on status change.

        Called for: TAKEOFF, LANDING, ALARM, OUTLANDING, EMERGENCY, DIVERTED.
        """
        if new_status not in EVENT_STATUSES:
            return

        try:
            db = get_db()
            await self._upsert_flight_status(db, flight)

            # Archive to flight_log on terminal states
            if new_status in (FlightStatus.LANDING, FlightStatus.OUTLANDING,
                              FlightStatus.DIVERTED):
                await self._write_flight_log(db, flight, new_status)

            log.debug(
                "pg_immediate_write",
                flarm_id=flight.flarm_id,
                status=new_status.name,
            )
        except Exception:
            log.exception("pg_status_write_failed", flarm_id=flight.flarm_id)

    async def periodic_sync(self, get_all_flights: callable,
                            shutdown_event: asyncio.Event) -> None:
        """Periodically bulk-sync all active flights to PostgreSQL.

        Args:
            get_all_flights: Callable returning list of all active FlightState objects.
            shutdown_event: Event to signal shutdown.
        """
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=self._sync_interval,
                )
                break
            except asyncio.TimeoutError:
                pass

            try:
                flights = get_all_flights()
                if flights:
                    await self._bulk_sync(flights)
            except Exception:
                log.exception("pg_periodic_sync_failed")

    async def _bulk_sync(self, flights: list[FlightState]) -> None:
        """Bulk-UPDATE flight_status table from in-memory state."""
        db = get_db()
        async with db.acquire() as conn:
            for flight in flights:
                try:
                    await conn.execute(
                        """
                        INSERT INTO flight_status (
                            airfield_id, flarm_id, registration, status,
                            latitude, longitude, altitude_m, altitude_agl,
                            speed_kmh, vertical_speed_ms, track_deg,
                            distance_m, qdr_deg, bearing_text,
                            takeoff_time, landing_time, last_seen,
                            max_altitude_m, max_distance_m,
                            launch_type, tow_plane_flarm_id, release_altitude_m,
                            updated_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12, $13, $14, $15, $16, $17, $18, $19,
                            $20, $21, $22, NOW()
                        )
                        ON CONFLICT (airfield_id, flarm_id) DO UPDATE SET
                            registration = EXCLUDED.registration,
                            status = EXCLUDED.status,
                            latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude,
                            altitude_m = EXCLUDED.altitude_m,
                            altitude_agl = EXCLUDED.altitude_agl,
                            speed_kmh = EXCLUDED.speed_kmh,
                            vertical_speed_ms = EXCLUDED.vertical_speed_ms,
                            track_deg = EXCLUDED.track_deg,
                            distance_m = EXCLUDED.distance_m,
                            qdr_deg = EXCLUDED.qdr_deg,
                            bearing_text = EXCLUDED.bearing_text,
                            takeoff_time = EXCLUDED.takeoff_time,
                            landing_time = EXCLUDED.landing_time,
                            last_seen = EXCLUDED.last_seen,
                            max_altitude_m = EXCLUDED.max_altitude_m,
                            max_distance_m = EXCLUDED.max_distance_m,
                            launch_type = EXCLUDED.launch_type,
                            tow_plane_flarm_id = EXCLUDED.tow_plane_flarm_id,
                            release_altitude_m = EXCLUDED.release_altitude_m,
                            updated_at = NOW()
                        """,
                        flight.airfield_id,
                        flight.flarm_id,
                        flight.registration,
                        flight.status.value,
                        flight.latitude,
                        flight.longitude,
                        flight.altitude_m,
                        flight.altitude_agl,
                        flight.speed_kmh,
                        flight.vertical_speed_ms,
                        flight.track_deg,
                        flight.distance_m,
                        flight.qdr_deg,
                        flight.bearing_text,
                        _parse_iso_or_none(flight.takeoff_time),
                        _parse_iso_or_none(flight.landing_time),
                        _parse_iso_or_none(flight.last_seen),
                        flight.max_altitude_m,
                        flight.max_distance_m,
                        flight.launch_type,
                        flight.tow_plane_flarm_id or None,
                        flight.release_alt_m or None,
                    )
                except Exception:
                    log.exception(
                        "pg_sync_flight_failed",
                        flarm_id=flight.flarm_id,
                    )

        log.debug("pg_bulk_sync_complete", count=len(flights))

    async def _upsert_flight_status(self, db, flight: FlightState) -> None:
        """Upsert a single flight status to PostgreSQL."""
        await db.execute(
            """
            INSERT INTO flight_status (
                airfield_id, flarm_id, registration, status,
                latitude, longitude, altitude_m, altitude_agl,
                speed_kmh, vertical_speed_ms, track_deg,
                distance_m, qdr_deg, bearing_text,
                takeoff_time, landing_time, last_seen,
                max_altitude_m, max_distance_m,
                launch_type, tow_plane_flarm_id, release_altitude_m,
                updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17, $18, $19,
                $20, $21, $22, NOW()
            )
            ON CONFLICT (airfield_id, flarm_id) DO UPDATE SET
                status = EXCLUDED.status,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                altitude_m = EXCLUDED.altitude_m,
                last_seen = EXCLUDED.last_seen,
                updated_at = NOW()
            """,
            flight.airfield_id,
            flight.flarm_id,
            flight.registration,
            flight.status.value,
            flight.latitude,
            flight.longitude,
            flight.altitude_m,
            flight.altitude_agl,
            flight.speed_kmh,
            flight.vertical_speed_ms,
            flight.track_deg,
            flight.distance_m,
            flight.qdr_deg,
            flight.bearing_text,
            _parse_iso_or_none(flight.takeoff_time),
            _parse_iso_or_none(flight.landing_time),
            _parse_iso_or_none(flight.last_seen),
            flight.max_altitude_m,
            flight.max_distance_m,
            flight.launch_type,
            flight.tow_plane_flarm_id or None,
            flight.release_alt_m or None,
        )

    async def _write_flight_log(self, db, flight: FlightState,
                                status: FlightStatus) -> None:
        """Archive a completed flight to the flight_log table."""
        landing_type = "home"
        if status == FlightStatus.OUTLANDING:
            landing_type = "outlanding"
        elif status == FlightStatus.DIVERTED:
            landing_type = "diverted"

        try:
            await db.execute(
                """
                INSERT INTO flight_log (
                    airfield_id, flarm_id, registration, aircraft_model,
                    competition_sign, takeoff_time, landing_time,
                    max_altitude_m, max_distance_m,
                    launch_type, landing_type,
                    landing_latitude, landing_longitude,
                    tow_plane_flarm_id, release_altitude_m
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
                )
                ON CONFLICT (airfield_id, flarm_id, takeoff_time) DO UPDATE SET
                    landing_time = COALESCE(EXCLUDED.landing_time, flight_log.landing_time),
                    max_altitude_m = GREATEST(EXCLUDED.max_altitude_m, flight_log.max_altitude_m),
                    max_distance_m = GREATEST(EXCLUDED.max_distance_m, flight_log.max_distance_m),
                    launch_type = COALESCE(NULLIF(EXCLUDED.launch_type, 'unknown'), flight_log.launch_type),
                    landing_type = EXCLUDED.landing_type
                """,
                flight.airfield_id,
                flight.flarm_id,
                flight.registration,
                flight.aircraft_model,
                flight.competition_sign,
                _parse_iso_or_none(flight.takeoff_time),
                _parse_iso_or_none(flight.landing_time),
                flight.max_altitude_m,
                flight.max_distance_m,
                flight.launch_type,
                landing_type,
                flight.latitude if landing_type != "home" else None,
                flight.longitude if landing_type != "home" else None,
                flight.tow_plane_flarm_id or None,
                flight.release_alt_m or None,
            )
            log.info(
                "flight_archived",
                flarm_id=flight.flarm_id,
                landing_type=landing_type,
            )
        except Exception:
            log.exception("flight_archive_failed", flarm_id=flight.flarm_id)


def _parse_iso_or_none(iso_str: str):
    """Parse ISO datetime string to datetime or return None."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
