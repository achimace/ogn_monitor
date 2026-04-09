"""APRS Worker - OGN Data Ingestion Entry Point.

This is the SINGLETON worker process that handles:
- APRS-IS TCP connection to aprs.glidernet.org
- Beacon parsing and flight tracking
- Flight state machine + profile analysis
- Launch type detection
- Writing results to Redis (Hot State)
- Periodic sync to PostgreSQL (Cold Storage)

Started via: python -m app.worker
IMPORTANT: Only ONE instance must run at a time!
"""

import asyncio
import logging
import signal
import sys

import structlog

from app.config import settings
from app.db.connection import init_db, close_db, get_db
from app.redis_client import init_redis, close_redis, get_redis

# Set root logging level
logging.basicConfig(
    format="%(message)s",
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

log = structlog.get_logger()

# Shutdown flag
shutdown_event = asyncio.Event()


def handle_signal(sig, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown."""
    log.info("Received shutdown signal", signal=sig)
    shutdown_event.set()


async def load_airfield_configs() -> dict:
    """Load active airfield configurations from PostgreSQL."""
    from app.tracking.flight_state_machine import AirfieldConfig

    db = get_db()
    rows = await db.fetch(
        "SELECT id, slug, latitude, longitude, elevation_m, "
        "home_radius_m, ogn_filter_radius_km, alarm_timeout_s, "
        "signal_loss_timeout_s, takeoff_speed_kmh, takeoff_alt_offset_m, "
        "tow_plane_flarm_ids, winch_vs_threshold_ms, "
        "landed_visible_minutes, "
        "ST_AsGeoJSON(home_polygon) AS home_polygon_geojson "
        "FROM airfields WHERE is_active = TRUE"
    )

    configs = {}
    for row in rows:
        slug = row["slug"]
        tow_ids = row["tow_plane_flarm_ids"]
        if isinstance(tow_ids, str):
            tow_ids = [x.strip() for x in tow_ids.split(",") if x.strip()]

        # Parse optional home polygon (GeoJSON -> shapely)
        home_polygon = None
        if row["home_polygon_geojson"]:
            try:
                import json
                from shapely.geometry import shape
                home_polygon = shape(json.loads(row["home_polygon_geojson"]))
            except Exception as e:
                log.warning("home_polygon_parse_failed", slug=slug, error=str(e))

        configs[slug] = AirfieldConfig(
            id=row["id"],
            slug=slug,
            latitude=row["latitude"],
            longitude=row["longitude"],
            elevation_m=row["elevation_m"],
            home_radius_m=row["home_radius_m"] or settings.home_radius_m,
            takeoff_speed_kmh=row["takeoff_speed_kmh"] or settings.takeoff_speed_threshold_kmh,
            takeoff_alt_offset_m=row["takeoff_alt_offset_m"] or settings.takeoff_altitude_offset_m,
            alarm_timeout_s=row["alarm_timeout_s"] or settings.alarm_timeout_s,
            signal_loss_timeout_s=row["signal_loss_timeout_s"] or settings.signal_loss_timeout_s,
            ogn_filter_radius_km=row["ogn_filter_radius_km"] or settings.ogn_default_radius_km,
            tow_plane_flarm_ids=tow_ids,
            winch_vs_threshold_ms=row["winch_vs_threshold_ms"] or 8.0,
            home_polygon=home_polygon,
            sticky_landed_max_age_s=(row["landed_visible_minutes"] or 1440) * 60,
        )

    log.info("airfield_configs_loaded", count=len(configs))
    return configs


async def main():
    """Main worker loop."""
    log.info(
        "Starting OGN FlightMonitor APRS Worker",
        version="0.2.0",
        callsign=settings.ogn_callsign,
        server=f"{settings.ogn_server}:{settings.ogn_port}",
    )

    # Initialize connections
    await init_db()
    log.info("PostgreSQL connection pool initialized")

    redis = await init_redis()
    log.info("Redis connection initialized")

    # Write initial health status
    await redis.hset("ogn:health", mapping={
        "connected": "false",
        "status": "starting",
        "beacons_per_minute": "0",
        "reconnect_count": "0",
    })

    try:
        # Import components
        from app.aprs.client import APRSClient
        from app.aprs.filter_builder import AirfieldPosition, build_filters
        from app.data.aircraft_resolver import AircraftResolver
        from app.data.ddb_updater import scheduled_sync, sync_ddb
        from app.tracking.flight_tracker import FlightTracker
        from app.tracking.redis_writer import RedisWriter
        from app.tracking.state_synchronizer import StateSynchronizer

        # Initialize components
        redis_writer = RedisWriter(redis)
        aircraft_resolver = AircraftResolver()
        flight_tracker = FlightTracker(redis_writer, aircraft_resolver)
        state_sync = StateSynchronizer()

        # Load airfield configs
        configs = await load_airfield_configs()
        flight_tracker.set_configs(configs)

        if not configs:
            log.warning("No active airfields configured - worker will idle")

        # Load aircraft cache
        await aircraft_resolver.load()

        # Initial DDB sync (if registry is empty)
        db = get_db()
        count = await db.fetchval("SELECT COUNT(*) FROM aircraft_registry")
        if count == 0:
            log.info("Aircraft registry empty - running initial DDB sync")
            await sync_ddb()
            await aircraft_resolver.load()

        # Recover flights from Redis (if worker restarted)
        recovered = await flight_tracker.recover_from_redis()
        if recovered:
            log.info("Recovered active flights from Redis", count=recovered)

        # Build APRS filters from airfield positions
        filter_airfields = [
            AirfieldPosition(
                slug=slug,
                latitude=cfg.latitude,
                longitude=cfg.longitude,
                radius_km=cfg.ogn_filter_radius_km,
            )
            for slug, cfg in configs.items()
        ]
        aprs_filters = build_filters(filter_airfields)

        if not aprs_filters:
            log.warning("No APRS filters - no airfields configured")
            aprs_filters = [f"r/48.0/11.0/100"]  # Default: Bavaria fallback

        # Health callback: write to Redis
        async def on_health_change(health: dict):
            await redis_writer.update_health(health)

        # Create APRS client
        aprs_client = APRSClient(
            callsign=settings.ogn_callsign,
            on_beacon=flight_tracker.process_line,
            on_health_change=on_health_change,
        )

        log.info("Worker components initialized")

        # Start background tasks
        tasks = []

        # APRS client (main data stream)
        tasks.append(asyncio.create_task(
            aprs_client.run(aprs_filters),
            name="aprs_client",
        ))

        # Periodic timeout checks + health update (every 30s)
        async def timeout_loop():
            while not shutdown_event.is_set():
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                    break
                except asyncio.TimeoutError:
                    pass
                try:
                    await flight_tracker.check_timeouts()
                    # Update OGN health in Redis
                    await redis_writer.update_health(aprs_client.health_status())
                except Exception:
                    log.exception("timeout_check_failed")

        tasks.append(asyncio.create_task(
            timeout_loop(),
            name="timeout_checker",
        ))

        # Periodic state sync to PostgreSQL
        tasks.append(asyncio.create_task(
            state_sync.periodic_sync(
                flight_tracker.state_machine.get_all_active_flights,
                shutdown_event,
            ),
            name="state_sync",
        ))

        # Aircraft cache reload
        tasks.append(asyncio.create_task(
            aircraft_resolver.periodic_reload(shutdown_event),
            name="cache_reload",
        ))

        # DDB scheduled sync
        tasks.append(asyncio.create_task(
            scheduled_sync(shutdown_event),
            name="ddb_sync",
        ))

        # Periodic airfield config reload (every 5 min)
        async def config_reload_loop():
            while not shutdown_event.is_set():
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=300)
                    break
                except asyncio.TimeoutError:
                    pass
                try:
                    new_configs = await load_airfield_configs()
                    flight_tracker.set_configs(new_configs)

                    # Rebuild APRS filters if airfields changed
                    new_filter_airfields = [
                        AirfieldPosition(
                            slug=slug,
                            latitude=cfg.latitude,
                            longitude=cfg.longitude,
                            radius_km=cfg.ogn_filter_radius_km,
                        )
                        for slug, cfg in new_configs.items()
                    ]
                    new_filters = build_filters(new_filter_airfields)
                    if new_filters and new_filters != aprs_filters:
                        await aprs_client.update_filters(new_filters)
                except Exception:
                    log.exception("config_reload_failed")

        tasks.append(asyncio.create_task(
            config_reload_loop(),
            name="config_reload",
        ))

        log.info(
            "Worker fully started",
            tasks=[t.get_name() for t in tasks],
            airfields=list(configs.keys()),
            filters=aprs_filters,
        )

        # Wait for shutdown signal
        await shutdown_event.wait()

        # Graceful shutdown
        log.info("Shutting down worker tasks...")
        await aprs_client.stop()

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

    except Exception:
        log.exception("Worker crashed")
        raise
    finally:
        log.info("Shutting down APRS Worker")
        await redis.hset("ogn:health", mapping={
            "connected": "false",
            "status": "shutdown",
        })
        await close_redis()
        await close_db()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    sys.exit(0)
