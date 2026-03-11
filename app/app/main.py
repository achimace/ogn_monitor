"""FastAPI Application - API Server Entry Point.

This is the API server that handles:
- REST API (Auth, CRUD, Monitor data)
- WebSocket connections (real-time flight updates)
- Redis PubSub listener for broadcasting APRS Worker updates

Started via: uvicorn app.main:app --workers 4
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.connection import init_db, close_db
from app.redis_client import init_redis, close_redis

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

# PubSub listener task (per worker process)
_pubsub_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown."""
    global _pubsub_task

    # Startup
    log.info("Starting OGN FlightMonitor API Server", version="0.2.0")

    await init_db()
    log.info("PostgreSQL connection pool initialized")

    await init_redis()
    log.info("Redis connection initialized")

    # Start Redis PubSub listener for WebSocket broadcast
    from app.api.pubsub_listener import start_pubsub_listener
    _pubsub_task = asyncio.create_task(
        start_pubsub_listener(),
        name="pubsub_listener",
    )
    log.info("Redis PubSub listener started")

    yield

    # Shutdown
    log.info("Shutting down OGN FlightMonitor API Server")
    if _pubsub_task:
        _pubsub_task.cancel()
        try:
            await _pubsub_task
        except asyncio.CancelledError:
            pass
    await close_redis()
    await close_db()


app = FastAPI(
    title="OGN FlightMonitor",
    description="Echtzeit-Flugmonitoring fuer Segelflugplaetze",
    version="0.2.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Health Endpoints ---

@app.get("/health")
async def health():
    """Health check for Docker."""
    return {"status": "ok", "service": "api"}


@app.get("/api/health/ogn")
async def ogn_health():
    """OGN worker health - reads status from Redis."""
    from app.redis_client import get_redis
    r = get_redis()
    health_data = await r.hgetall("ogn:health")
    if not health_data:
        return {"connected": False, "message": "No health data from worker"}
    return health_data


# --- API Routers ---

from app.api.auth import router as auth_router
from app.api.tenants import router as tenants_router
from app.api.airfields import router as airfields_router
from app.api.aircraft import router as aircraft_router
from app.api.monitor import router as monitor_router
from app.api.websocket import router as ws_router
from app.api.flight_log import router as flight_log_router

app.include_router(auth_router)
app.include_router(tenants_router)
app.include_router(airfields_router)
app.include_router(aircraft_router)
app.include_router(monitor_router)
app.include_router(ws_router)
app.include_router(flight_log_router)
