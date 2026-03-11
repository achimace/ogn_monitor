"""APRS Worker - OGN Data Ingestion Entry Point.

This is the SINGLETON worker process that handles:
- APRS-IS TCP connection to aprs.glidernet.org
- Beacon parsing and flight tracking
- Flight state machine + profile analysis
- Launch type detection
- Writing results to Redis (Hot State)

Started via: python -m app.worker
IMPORTANT: Only ONE instance must run at a time!
"""

import asyncio
import logging
import signal
import sys

import structlog

from app.config import settings
from app.db.connection import init_db, close_db
from app.redis_client import init_redis, close_redis

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


async def main():
    """Main worker loop."""
    log.info(
        "Starting OGN FlightMonitor APRS Worker",
        version="0.1.0",
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
        # TODO Phase 2: Initialize components
        # - AircraftResolver (load cache from DB)
        # - AirfieldManager (load active airfields)
        # - FlightTracker
        # - APRSClient (connect and start streaming)

        log.info("Worker initialized - waiting for APRS client implementation")

        # Placeholder: wait for shutdown
        await shutdown_event.wait()

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
