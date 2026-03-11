"""FastAPI Application - API Server Entry Point.

This is the API server that handles:
- REST API (Auth, CRUD, Monitor data)
- WebSocket connections (real-time flight updates)
- Redis -> PostgreSQL state synchronization

Started via: uvicorn app.main:app --workers 4
"""

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown."""
    # Startup
    log.info("Starting OGN FlightMonitor API Server", version="0.1.0")

    await init_db()
    log.info("PostgreSQL connection pool initialized")

    await init_redis()
    log.info("Redis connection initialized")

    # TODO: Start Redis PubSub subscriber for WebSocket broadcast
    # TODO: Start periodic Redis -> PostgreSQL state sync

    yield

    # Shutdown
    log.info("Shutting down OGN FlightMonitor API Server")
    await close_redis()
    await close_db()


app = FastAPI(
    title="OGN FlightMonitor",
    description="Echtzeit-Flugmonitoring fuer Segelflugplaetze",
    version="0.1.0",
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


# --- API Routers (added incrementally) ---

# TODO Phase 1: from app.api.auth import router as auth_router
# TODO Phase 1: from app.api.tenants import router as tenants_router
# TODO Phase 1: from app.api.airfields import router as airfields_router
# TODO Phase 1: from app.api.aircraft import router as aircraft_router
# TODO Phase 3: from app.api.monitor import router as monitor_router
# TODO Phase 3: from app.api.websocket import router as ws_router
# TODO Phase 5: from app.api.flights import router as flights_router
# TODO Phase 6: from app.api.admin import router as admin_router
